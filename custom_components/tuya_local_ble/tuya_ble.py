from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from weakref import WeakSet
from collections.abc import Callable
from struct import pack, unpack

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak_retry_connector import BLEAK_BACKOFF_TIME
from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakError,
    BleakNotFoundError,
    establish_connection,
)
from Crypto.Cipher import AES

from .const import (
    CHARACTERISTIC_NOTIFY,
    CHARACTERISTIC_WRITE,
    GATT_MTU,
    MANUFACTURER_DATA_ID,
    RESPONSE_WAIT_TIMEOUT,
    SERVICE_UUID,
    TuyaBLECode,
    TuyaBLEDataPointType,
)
from .exceptions import (
    TuyaBLEDataCRCError,
    TuyaBLEDataFormatError,
    TuyaBLEDataLengthError,
    TuyaBLEDeviceError,
    TuyaBLEEnumValueError,
)
from .manager import AbstaractTuyaBLEDeviceManager, TuyaBLEDeviceCredentials

_LOGGER = logging.getLogger(__name__)


BLEAK_EXCEPTIONS = (*BLEAK_RETRY_EXCEPTIONS, OSError)

# Tuya packets are fragmented into 20-byte GATT writes. A short pause between
# write-without-response operations prevents overrunning slower lock firmware.
INTER_PACKET_DELAY = 0.02
SEND_RETRY_ATTEMPTS = 2
CONNECT_ATTEMPTS = 3
CONNECT_RETRY_DELAY = 3.0
DISCONNECT_TIMEOUT = 10.0


class TuyaBLEDataPoint:
    def __init__(
        self,
        owner: TuyaBLEDataPoints,
        id: int,
        timestamp: float,
        flags: int,
        type: TuyaBLEDataPointType,
        value: bytes | bool | int | str,
    ) -> None:
        self._owner = owner
        self._id = id
        self._value = value
        self._changed_by_device = False
        self._update_from_device(timestamp, flags, type, value)

    def _update_from_device(
        self,
        timestamp: float,
        flags: int,
        type: TuyaBLEDataPointType,
        value: bytes | bool | int | str,
    ) -> None:
        self._timestamp = timestamp
        self._flags = flags
        self._type = type
        self._changed_by_device = self._value != value
        self._value = value

    def _get_value(self) -> bytes:
        match self._type:
            case TuyaBLEDataPointType.DT_RAW | TuyaBLEDataPointType.DT_BITMAP:
                return self._value
            case TuyaBLEDataPointType.DT_BOOL:
                return pack(">B", 1 if self._value else 0)
            case TuyaBLEDataPointType.DT_VALUE:
                return pack(">i", self._value)
            case TuyaBLEDataPointType.DT_ENUM:
                if self._value > 0xFFFF:
                    return pack(">I", self._value)
                elif self._value > 0xFF:
                    return pack(">H", self._value)
                else:
                    return pack(">B", self._value)
            case TuyaBLEDataPointType.DT_STRING:
                return self._value.encode()

    @property
    def id(self) -> int:
        return self._id

    @property
    def timestamp(self) -> float:
        return self._timestamp

    @property
    def flags(self) -> int:
        return self._flags

    @property
    def type(self) -> TuyaBLEDataPointType:
        return self._type

    @property
    def value(self) -> bytes | bool | int | str:
        return self._value

    @property
    def changed_by_device(self) -> bool:
        return self._changed_by_device

    async def set_value(self, value: bytes | bool | int | str) -> None:
        match self._type:
            case TuyaBLEDataPointType.DT_RAW | TuyaBLEDataPointType.DT_BITMAP:
                self._value = bytes(value)
            case TuyaBLEDataPointType.DT_BOOL:
                self._value = bool(value)
            case TuyaBLEDataPointType.DT_VALUE:
                self._value = int(value)
            case TuyaBLEDataPointType.DT_ENUM:
                value = int(value)
                if value >= 0:
                    self._value = value
                else:
                    raise TuyaBLEEnumValueError()

            case TuyaBLEDataPointType.DT_STRING:
                self._value = str(value)

        self._changed_by_device = False
        await self._owner._update_from_user(self._id)


class TuyaBLEDataPoints:
    def __init__(self, owner: TuyaBLEDevice) -> None:
        self._owner = owner
        self._datapoints: dict[int, TuyaBLEDataPoint] = {}
        self._update_started: int = 0
        self._updated_datapoints: list[int] = []

    def __len__(self) -> int:
        return len(self._datapoints)

    def __getitem__(self, key: int) -> TuyaBLEDataPoint | None:
        return self._datapoints.get(key)

    def has_id(self, id: int, type: TuyaBLEDataPointType | None = None) -> bool:
        return (id in self._datapoints) and (
            (type is None) or (self._datapoints[id].type == type)
        )

    def get_or_create(
        self,
        id: int,
        type: TuyaBLEDataPointType,
        value: bytes | bool | int | str | None = None,
    ) -> TuyaBLEDataPoint:
        datapoint = self._datapoints.get(id)
        if datapoint:
            return datapoint
        datapoint = TuyaBLEDataPoint(self, id, time.time(), 0, type, value)
        self._datapoints[id] = datapoint
        return datapoint

    def begin_update(self) -> None:
        self._update_started += 1

    async def end_update(self) -> None:
        if self._update_started > 0:
            self._update_started -= 1
            if self._update_started == 0 and len(self._updated_datapoints) > 0:
                await self._owner._send_datapoints(self._updated_datapoints)
                self._updated_datapoints = []

    def _update_from_device(
        self,
        dp_id: int,
        timestamp: float,
        flags: int,
        type: TuyaBLEDataPointType,
        value: bytes | bool | int | str,
    ) -> None:
        dp = self._datapoints.get(dp_id)
        if dp:
            dp._update_from_device(timestamp, flags, type, value)
        else:
            self._datapoints[dp_id] = TuyaBLEDataPoint(
                self, dp_id, timestamp, flags, type, value
            )

    async def _update_from_user(self, dp_id: int) -> None:
        if self._update_started > 0:
            if dp_id in self._updated_datapoints:
                self._updated_datapoints.remove(dp_id)
            self._updated_datapoints.append(dp_id)
        else:
            await self._owner._send_datapoints([dp_id])


global_connect_lock = asyncio.Lock()


class TuyaBLEDevice:
    def __init__(
        self,
        device_manager: AbstaractTuyaBLEDeviceManager,
        ble_device: BLEDevice,
        advertisement_data: AdvertisementData | None = None,
    ) -> None:
        """Init the TuyaBLE."""
        self._device_manager = device_manager
        self._device_info: TuyaBLEDeviceCredentials | None = None
        self._ble_device = ble_device
        self._advertisement_data = advertisement_data
        self._operation_lock = asyncio.Lock()
        self._transaction_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._client: BleakClientWithServiceCache | None = None
        self._expected_disconnect = False
        # BLE_DIAG: monotonic identifiers are scoped to this integration instance.
        self._ble_diag_next_session = 0
        self._ble_diag_notify_registrations = 0
        self._ble_diag_active_session: int | None = None
        self._ble_diag_sessions: dict[int, dict] = {}
        self._reconnect_task: asyncio.Task[None] | None = None
        self._discovery_update_task: asyncio.Task[None] | None = None
        # Track BlueZ D-Bus cleanup independently of the current BLE client.
        # A physical disconnect does not necessarily close Bleak's D-Bus bus.
        self._client_close_tasks: dict[
            BleakClientWithServiceCache, asyncio.Task[None]
        ] = {}
        self._disconnect_cleanup_tasks: set[asyncio.Task[None]] = set()
        # Remember buses already closed successfully without retaining clients.
        self._closed_ble_clients: WeakSet[BleakClientWithServiceCache] = WeakSet()
        self._scheduled_client_cleanups: dict[
            BleakClientWithServiceCache, asyncio.Task[None]
        ] = {}
        self._connected_callbacks: list[Callable[[], None]] = []
        self._callbacks: list[Callable[[list[TuyaBLEDataPoint]], None]] = []
        self._disconnected_callbacks: list[Callable[[], None]] = []
        self._current_seq_num = 1
        self._seq_num_lock = asyncio.Lock()

        self._is_bound = False
        self._flags = 0
        self._protocol_version = 2

        self._device_version: str = ""
        self._protocol_version_str: str = ""
        self._hardware_version: str = ""

        self._device_info: TuyaBLEDeviceCredentials | None = None

        self._auth_key: bytes | None = None
        self._local_key: bytes | None = None
        self._login_key: bytes | None = None
        self._session_key: bytes | None = None

        self._is_paired = False

        self._input_buffer: bytearray | None = None
        self._input_expected_packet_num = 0
        self._input_expected_length = 0
        self._input_expected_responses: dict[int,
                                             asyncio.Future[int] | None] = {}
        # self._input_future: asyncio.Future[int] | None = None

        self._datapoints = TuyaBLEDataPoints(self)

    def set_ble_device_and_advertisement_data(
        self, ble_device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """Set the ble device."""
        self._ble_device = ble_device
        self._advertisement_data = advertisement_data

    async def initialize(self) -> None:
        _LOGGER.debug("%s: Initializing", self.address)
        if await self._update_device_info():
            self._decode_advertisement_data()
            
    def _build_pairing_request(self) -> bytes:
        result = bytearray()

        result += self._device_info.uuid.encode()
        result += self._local_key
        result += self._device_info.device_id.encode()
        for _ in range(44 - len(result)):
            result += b"\x00"

        return result

    async def pair(self) -> None:
        """
        _LOGGER.debug("%s: Sending pairing request: %s",
            self.address, data.hex()
        )
        """
        await self._send_packet(
            TuyaBLECode.FUN_SENDER_PAIR, self._build_pairing_request()
        )

    async def update(self) -> None:
        _LOGGER.debug("%s: Updating", self.address)
        await self._send_packet(TuyaBLECode.FUN_SENDER_DEVICE_STATUS, bytes())

    async def _update_device_info(self) -> bool:
        if self._device_info is None:
            if self._device_manager:
                self._device_info = await self._device_manager.get_device_credentials(
                    self._ble_device.address, False
                )
            if self._device_info:
                self._local_key = self._device_info.local_key[:6].encode()
                self._login_key = hashlib.md5(self._local_key).digest()

        return self._device_info is not None

    def _decode_advertisement_data(self) -> None:
        raw_product_id: bytes | None = None
        # raw_product_key: bytes | None = None
        raw_uuid: bytes | None = None
        if self._advertisement_data:
            if self._advertisement_data.service_data:
                service_data = self._advertisement_data.service_data.get(
                    SERVICE_UUID)
                if service_data and len(service_data) > 1:
                    match service_data[0]:
                        case 0:
                            raw_product_id = service_data[1:]
                        # case 1:
                        #    raw_product_key = service_data[1:]

            if self._advertisement_data.manufacturer_data:
                manufacturer_data = self._advertisement_data.manufacturer_data.get(
                    MANUFACTURER_DATA_ID
                )
                if manufacturer_data and len(manufacturer_data) > 6:
                    self._is_bound = (manufacturer_data[0] & 0x80) != 0
                    self._protocol_version = manufacturer_data[1]
                    raw_uuid = manufacturer_data[6:]
                    if raw_product_id:
                        key = hashlib.md5(raw_product_id).digest()
                        cipher = AES.new(key, AES.MODE_CBC, key)
                        raw_uuid = cipher.decrypt(raw_uuid)
                        self._uuid = raw_uuid.decode("utf-8")

    @property
    def address(self) -> str:
        """Return the address."""
        return self._ble_device.address

    @property
    def name(self) -> str:
        """Get the name of the device."""
        if self._device_info:
            return self._device_info.device_name
        else:
            return self._ble_device.name or self._ble_device.address

    @property
    def rssi(self) -> int | None:
        """Get the rssi of the device."""
        if self._advertisement_data:
            return self._advertisement_data.rssi
        return None

    @property
    def uuid(self) -> str:
        if self._device_info is not None:
            return self._device_info.uuid
        else:
            return ""

    @property
    def local_key(self) -> str:
        if self._device_info is not None:
            return self._device_info.local_key
        else:
            return ""

    @property
    def category(self) -> str:
        if self._device_info is not None:
            return self._device_info.category
        else:
            return ""

    @property
    def device_id(self) -> str:
        if self._device_info is not None:
            return self._device_info.device_id
        else:
            return ""

    @property
    def product_id(self) -> str:
        if self._device_info is not None:
            return self._device_info.product_id
        else:
            return ""

    @property
    def product_model(self) -> str:
        if self._device_info is not None:
            return self._device_info.product_model
        else:
            return ""

    @property
    def product_name(self) -> str:
        if self._device_info is not None:
            return self._device_info.product_name
        else:
            return ""

    @property
    def device_version(self) -> str:
        return self._device_version

    @property
    def hardware_version(self) -> str:
        return self._hardware_version

    @property
    def protocol_version(self) -> str:
        return self._protocol_version_str

    @property
    def datapoints(self) -> TuyaBLEDataPoints:
        """Get datapoints exposed by device."""
        return self._datapoints

    @property
    def is_connected(self) -> bool:
        """Return whether the current BLE client is connected and paired."""
        client = self._client
        return bool(client and client.is_connected and self._is_paired)

    def get_or_create_datapoint(
        self,
        id: int,
        type: TuyaBLEDataPointType,
        value: bytes | bool | int | str | None = None,
    ) -> TuyaBLEDataPoint:
        """Get datapoints exposed by device."""

    def _fire_connected_callbacks(self) -> None:
        """Fire the callbacks."""
        for callback in self._connected_callbacks:
            callback()

    def register_connected_callback(
        self, callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Register a callback to be called when device disconnected."""

        def unregister_callback() -> None:
            self._connected_callbacks.remove(callback)

        self._connected_callbacks.append(callback)
        return unregister_callback

    def _fire_callbacks(self, datapoints: list[TuyaBLEDataPoint]) -> None:
        """Fire the callbacks."""
        for callback in self._callbacks:
            callback(datapoints)

    def register_callback(
        self,
        callback: Callable[[list[TuyaBLEDataPoint]], None],
    ) -> Callable[[], None]:
        """Register a callback to be called when the state changes."""

        def unregister_callback() -> None:
            self._callbacks.remove(callback)

        self._callbacks.append(callback)
        return unregister_callback

    def _fire_disconnected_callbacks(self) -> None:
        """Fire the callbacks."""
        for callback in self._disconnected_callbacks:
            callback()

    def register_disconnected_callback(
        self, callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Register a callback to be called when device disconnected."""

        def unregister_callback() -> None:
            self._disconnected_callbacks.remove(callback)

        self._disconnected_callbacks.append(callback)
        return unregister_callback

    async def start(self):
        """Start the TuyaBLE."""
        _LOGGER.debug("%s: Starting...", self.address)
        # await self._send_packet()

    async def stop(self) -> None:
        """Stop the TuyaBLE."""
        _LOGGER.debug("%s: Stop", self.address)
        self._expected_disconnect = True
        for task in (self._reconnect_task, self._discovery_update_task):
            if task is not None and not task.done():
                task.cancel()
        await self._execute_disconnect()

    def _schedule_reconnect(self) -> None:
        """Run at most one reconnect worker per lock."""
        if self._expected_disconnect:
            return
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect())

    def _invalidate_connection(
        self,
        reason: str,
        client: BleakClientWithServiceCache | None = None,
    ) -> bool:
        """Clear a failed connection without allowing stale callbacks to win."""
        current = self._client
        if client is not None and current is not None and client is not current:
            _LOGGER.debug(
                "%s: Ignoring stale disconnect for an old BLE client",
                self.address,
            )
            return False

        had_connection = current is not None or self._is_paired
        self._client = None
        self._is_paired = False
        self._abort_pending_responses(reason)
        # Some GATT write/error paths invalidate the client BEFORE BlueZ calls
        # _disconnected(). Never lose the only reference to its D-Bus bus.
        # During explicit shutdown, _execute_disconnect() closes it itself
        # after the optional StopNotify call.
        if current is not None and not self._expected_disconnect:
            self._schedule_client_cleanup(current, reason)
        if had_connection:
            self._fire_disconnected_callbacks()
        return had_connection

    def _ble_diag_register_notify(self, client: BleakClientWithServiceCache):
        """Create a traceable callback per GATT subscription (diagnostics only).

        No packets are discarded or modified by this wrapper. Session ids identify
        each registration, even if the same BleakClient object is reused.
        """
        self._ble_diag_next_session += 1
        session = self._ble_diag_next_session
        self._ble_diag_notify_registrations += 1
        stats = {
            "calls": 0,
            "duplicates": 0,
            "stale": 0,
            "last_payload": None,
            "last_time": 0.0,
            "client_id": f"0x{id(client):x}",
        }
        self._ble_diag_sessions[session] = stats
        self._ble_diag_active_session = session
        _LOGGER.warning(
            "%s: BLE_DIAG notify REGISTER session=%s client=%s "
            "total_registrations=%s",
            self.address, session, stats["client_id"],
            self._ble_diag_notify_registrations,
        )

        def traced_notification(sender: int, data: bytearray) -> None:
            stats["calls"] += 1
            now = time.monotonic()
            payload = bytes(data)
            duplicate = (
                stats["last_payload"] == payload
                and now - stats["last_time"] <= 0.25
            )
            if duplicate:
                stats["duplicates"] += 1
            stats["last_payload"] = payload
            stats["last_time"] = now

            current_session = self._ble_diag_active_session
            stale = client is not self._client or session != current_session
            if stale:
                stats["stale"] += 1

            # Limit diagnostic log volume while still exposing progression.
            calls = stats["calls"]
            report = (
                calls <= 3 or calls in (10, 25, 50, 100)
                or calls % 250 == 0
                or (duplicate and stats["duplicates"] in (1, 10, 50, 100))
                or (stale and stats["stale"] in (1, 10, 50, 100))
            )
            if report:
                _LOGGER.warning(
                    "%s: BLE_DIAG notify CALLBACK session=%s client=%s "
                    "active_session=%s active_client=%s calls=%s "
                    "duplicates_250ms=%s stale=%s repeat=%s "
                    "stale_call=%s fragment=%s payload_len=%s",
                    self.address, session, stats["client_id"],
                    current_session,
                    f"0x{id(self._client):x}" if self._client else "none",
                    calls, stats["duplicates"], stats["stale"],
                    duplicate, stale, payload[0] if payload else "empty",
                    len(payload),
                )
            # Diagnostic mode: preserve the existing packet-processing path.
            self._notification_handler(sender, data)

        return traced_notification

    def _ble_diag_close_session(
        self, client: BleakClientWithServiceCache | None, reason: str
    ) -> None:
        """Emit a summary for any session(s) tied to this Bleak client."""
        if client is None:
            return
        client_id = f"0x{id(client):x}"
        for session, stats in self._ble_diag_sessions.items():
            if stats["client_id"] != client_id or stats.get("closed"):
                continue
            stats["closed"] = True
            _LOGGER.warning(
                "%s: BLE_DIAG notify CLOSE session=%s client=%s reason=%s "
                "calls=%s duplicates_250ms=%s stale=%s",
                self.address, session, client_id, reason,
                stats["calls"], stats["duplicates"], stats["stale"],
            )
        if self._ble_diag_active_session is not None:
            active_stats = self._ble_diag_sessions.get(self._ble_diag_active_session)
            if active_stats and active_stats["client_id"] == client_id:
                self._ble_diag_active_session = None

    def _schedule_client_cleanup(
        self, client: BleakClientWithServiceCache, reason: str
    ) -> None:
        """Schedule one asynchronous D-Bus close for any abandoned client.

        This is synchronous so it is safe to call from Bleak's disconnect
        callback and from _invalidate_connection() without awaiting D-Bus IO.
        """
        if client in self._closed_ble_clients:
            return
        if client in self._scheduled_client_cleanups:
            return

        task = asyncio.create_task(self._close_client_bus(client, reason))
        self._scheduled_client_cleanups[client] = task
        self._disconnect_cleanup_tasks.add(task)
        _LOGGER.warning(
            "%s: BLE_CLEANUP scheduled old client=0x%x reason=%s",
            self.address, id(client), reason,
        )

        def finished(completed: asyncio.Task[None]) -> None:
            self._disconnect_cleanup_tasks.discard(completed)
            if self._scheduled_client_cleanups.get(client) is completed:
                self._scheduled_client_cleanups.pop(client, None)
            if not completed.cancelled():
                try:
                    completed.result()
                except Exception:
                    _LOGGER.exception(
                        "%s: BLE_CLEANUP task failed old client=0x%x",
                        self.address, id(client),
                    )

        task.add_done_callback(finished)

    def _disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Release even a client invalidated before BlueZ's callback arrives."""
        self._ble_diag_close_session(client, "BlueZ disconnected callback")
        # A stale callback must never invalidate or disconnect the NEW client;
        # it may, however, close its OWN old per-session D-Bus connection.
        self._schedule_client_cleanup(client, "BlueZ disconnected callback")

        if client is not self._client:
            _LOGGER.debug(
                "%s: BLE callback for an already invalidated/stale client=0x%x",
                self.address, id(client),
            )
            return

        was_paired = self._is_paired
        self._invalidate_connection(
            "BLE device unexpectedly disconnected", client
        )
        if was_paired and not self._expected_disconnect:
            # _ensure_connected() waits for queued D-Bus closes before
            # establishing the next BLE session.
            self._schedule_reconnect()

    async def _wait_for_disconnected_cleanups(self) -> None:
        """Wait for queued old-client closes before creating a new BLE client."""
        pending = [
            task for task in self._disconnect_cleanup_tasks if not task.done()
        ]
        if pending:
            await asyncio.gather(*pending)

    def _disconnect(self) -> None:
        """Disconnect from device."""
        asyncio.create_task(self._execute_timed_disconnect())

    async def _execute_timed_disconnect(self) -> None:
        """Execute timed disconnection."""
        _LOGGER.debug("%s: Disconnecting", self.address)
        await self._execute_disconnect()

    def _abort_pending_responses(self, reason: str) -> None:
        """Clear partial input and fail pending request/response waits."""
        self._clean_input()
        for future in list(self._input_expected_responses.values()):
            if future and not future.done():
                future.set_exception(BleakError(reason))
                # Mark the exception as retrieved even when the write failed
                # before the sending coroutine reached `await future`. Awaiters
                # still receive the same exception.
                future.exception()
        self._input_expected_responses.clear()

    async def _execute_disconnect(self) -> None:
        """Execute the final integration shutdown disconnection."""
        async with self._connect_lock:
            client = self._client
            self._expected_disconnect = True
            self._ble_diag_close_session(client, "integration stop")
            self._invalidate_connection("Disconnected from BLE device", client)
            if client is not None:
                if client.is_connected:
                    try:
                        await client.stop_notify(CHARACTERISTIC_NOTIFY)
                    except BLEAK_EXCEPTIONS:
                        _LOGGER.debug(
                            "%s: stop_notify failed during shutdown",
                            self.address,
                            exc_info=True,
                        )
                await self._close_client_bus(client, "integration stop")
        await self._wait_for_disconnected_cleanups()
        async with self._seq_num_lock:
            self._current_seq_num = 1

    async def _close_client_bus(
        self, client: BleakClientWithServiceCache, reason: str
    ) -> None:
        """Close a Bleak D-Bus bus once, even after the BLE link has dropped."""
        if client in self._closed_ble_clients:
            return
        task = self._client_close_tasks.get(client)
        if task is None:
            async def close() -> None:
                try:
                    # This must not be guarded by client.is_connected: Bleak's
                    # disconnect() also closes the per-session D-Bus bus.
                    await asyncio.wait_for(client.disconnect(), DISCONNECT_TIMEOUT)
                    self._closed_ble_clients.add(client)
                    _LOGGER.warning(
                        "%s: BLE_CLEANUP closed old client=0x%x reason=%s",
                        self.address, id(client), reason,
                    )
                except Exception:
                    _LOGGER.warning(
                        "%s: BLE_CLEANUP failed old client=0x%x reason=%s",
                        self.address, id(client), reason, exc_info=True,
                    )

            task = asyncio.create_task(close())
            self._client_close_tasks[client] = task
            def done(completed: asyncio.Task[None]) -> None:
                if self._client_close_tasks.get(client) is completed:
                    self._client_close_tasks.pop(client, None)
            task.add_done_callback(done)
        await asyncio.shield(task)

    async def _release_failed_client(
        self, client: BleakClientWithServiceCache, reason: str
    ) -> None:
        """Release both the physical connection and Bleak's D-Bus session."""
        self._ble_diag_close_session(client, reason)
        if self._client is client:
            self._invalidate_connection(reason, client)
        await self._close_client_bus(client, reason)

    async def _ensure_connected(self) -> None:
        """Connect with bounded retries and release failed sessions before retry."""
        if self._expected_disconnect or self.is_connected:
            return
        async with self._connect_lock:
            if self._expected_disconnect or self.is_connected:
                return
            await self._wait_for_disconnected_cleanups()
            if self._expected_disconnect or self.is_connected:
                return
            last_error: Exception | None = None
            for attempt in range(1, CONNECT_ATTEMPTS + 1):
                if self._expected_disconnect:
                    return
                client: BleakClientWithServiceCache | None = None
                success = False
                try:
                    async with global_connect_lock:
                        _LOGGER.debug(
                            "%s: Connecting attempt %s/%s; RSSI: %s",
                            self.address, attempt, CONNECT_ATTEMPTS, self.rssi,
                        )
                        client = await establish_connection(
                            BleakClientWithServiceCache,
                            self._ble_device,
                            self.address,
                            self._disconnected,
                            use_services_cache=True,
                            ble_device_callback=lambda: self._ble_device,
                        )
                    if self._expected_disconnect:
                        return
                    if not client.is_connected:
                        raise BleakError("BLE connection was not established")
                    self._client = client
                    self._is_paired = False
                    notify_callback = self._ble_diag_register_notify(client)
                    try:
                        await client.start_notify(
                            CHARACTERISTIC_NOTIFY, notify_callback
                        )
                    except BaseException:
                        self._ble_diag_close_session(client, "start_notify failed")
                        raise
                    _LOGGER.warning(
                        "%s: BLE_DIAG notify STARTED session=%s client=0x%x",
                        self.address, self._ble_diag_active_session, id(client),
                    )
                    if not await self._send_packet_while_connected(
                        TuyaBLECode.FUN_SENDER_DEVICE_INFO, bytes(0), 0, True
                    ):
                        raise BleakError("Device info response timed out")
                    if not await self._send_packet_while_connected(
                        TuyaBLECode.FUN_SENDER_PAIR,
                        self._build_pairing_request(), 0, True,
                    ):
                        raise BleakError("Pairing response timed out")
                    if not self.is_connected:
                        raise BleakError("Pairing was not completed")
                    success = True
                    self._fire_connected_callbacks()
                    _LOGGER.debug("%s: Connected and paired", self.address)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    last_error = err
                    _LOGGER.warning(
                        "%s: BLE connection attempt %s/%s failed: %s",
                        self.address, attempt, CONNECT_ATTEMPTS, err,
                    )
                finally:
                    if client is not None and not success:
                        await self._release_failed_client(
                            client, "BLE connection initialization failed"
                        )
                if attempt < CONNECT_ATTEMPTS and not self._expected_disconnect:
                    await asyncio.sleep(CONNECT_RETRY_DELAY)
            raise BleakError(
                f"BLE connection failed after {CONNECT_ATTEMPTS} attempts: {last_error}"
            )

    async def _reconnect(self) -> None:
        """A single reconnect worker with bounded backoff and clean shutdown."""
        async with self._seq_num_lock:
            self._current_seq_num = 1
        delay = max(float(BLEAK_BACKOFF_TIME), 3.0)
        while not self._expected_disconnect:
            try:
                await self._ensure_connected()
                if self.is_connected or self._expected_disconnect:
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.debug(
                    "%s: Reconnect failed; retrying in %.0f seconds",
                    self.address, delay, exc_info=True,
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)

    @staticmethod
    def _calc_crc16(data: bytes) -> int:
        crc = 0xFFFF
        for byte in data:
            crc ^= byte & 255
            for _ in range(8):
                tmp = crc & 1
                crc >>= 1
                if tmp != 0:
                    crc ^= 0xA001
        return crc

    @staticmethod
    def _pack_int(value: int) -> bytearray:
        curr_byte: int
        result = bytearray()
        while True:
            curr_byte = value & 0x7F
            value >>= 7
            if value != 0:
                curr_byte |= 0x80
            result += pack(">B", curr_byte)
            if value == 0:
                break
        return result

    @staticmethod
    def _unpack_int(data: bytes, start_pos: int) -> tuple(int, int):
        result: int = 0
        offset: int = 0
        while offset < 5:
            pos: int = start_pos + offset
            if pos >= len(data):
                raise TuyaBLEDataFormatError()
            curr_byte: int = data[pos]
            result |= (curr_byte & 0x7F) << (offset * 7)
            offset += 1
            if (curr_byte & 0x80) == 0:
                break
        if offset > 4:
            raise TuyaBLEDataFormatError()
        else:
            return (result, start_pos + offset)

    def _build_packets(
        self,
        seq_num: int,
        code: TuyaBLECode,
        data: bytes,
        response_to: int = 0,
    ) -> list[bytes]:
        key: bytes
        iv = secrets.token_bytes(16)
        security_flag: bytes
        if code == TuyaBLECode.FUN_SENDER_DEVICE_INFO:
            key = self._login_key
            security_flag = b"\x04"
        else:
            key = self._session_key
            security_flag = b"\x05"

        raw = bytearray()
        raw += pack(">IIHH", seq_num, response_to, code.value, len(data))
        raw += data
        crc = self._calc_crc16(raw)
        raw += pack(">H", crc)
        while len(raw) % 16 != 0:
            raw += b"\x00"

        cipher = AES.new(key, AES.MODE_CBC, iv)
        encrypted = security_flag + iv + cipher.encrypt(raw)

        command = []
        packet_num = 0
        pos = 0
        length = len(encrypted)
        while pos < length:
            packet = bytearray()
            packet += self._pack_int(packet_num)

            if packet_num == 0:
                packet += self._pack_int(length)
                packet += pack(">B", self._protocol_version << 4)

            data_part = encrypted[
                pos:pos + GATT_MTU - len(packet)  # fmt: skip
            ]
            packet += data_part
            command.append(packet)

            pos += len(data_part)
            packet_num += 1

        return command

    async def _get_seq_num(self) -> int:
        async with self._seq_num_lock:
            result = self._current_seq_num
            self._current_seq_num += 1
        return result

    async def _send_packet(
        self,
        code: TuyaBLECode,
        data: bytes,
        wait_for_response: bool = True,
    ) -> None:
        """Send a logical Tuya command, reconnecting and rebuilding once."""
        if self._expected_disconnect:
            return

        # Never automatically repeat a physical lock command after an ambiguous
        # GATT failure: it may already have been executed by the lock.
        attempts = (
            SEND_RETRY_ATTEMPTS
            if code == TuyaBLECode.FUN_SENDER_DEVICE_STATUS else 1
        )
        for attempt in range(attempts):
            try:
                await self._ensure_connected()
                if self._expected_disconnect:
                    return
                if not self.is_connected:
                    raise BleakError("BLE client is not connected and paired")
                result = await self._send_packet_while_connected(
                    code, data, 0, wait_for_response
                )
                if not result:
                    raise BleakError(f"No response to {code.name}")
                return
            except BLEAK_EXCEPTIONS as err:
                failed_client = self._client
                if failed_client is not None:
                    await self._release_failed_client(
                        failed_client, f"BLE command {code.name} failed: {err}"
                    )
                else:
                    self._invalidate_connection(
                        f"BLE command {code.name} failed: {err}"
                    )
                if attempt + 1 >= attempts:
                    raise
                _LOGGER.debug(
                    "%s: Retrying logical command %s after connection reset "
                    "(%s/%s); RSSI: %s",
                    self.address,
                    code.name,
                    attempt + 1,
                    attempts - 1,
                    self.rssi,
                )
                await asyncio.sleep(BLEAK_BACKOFF_TIME)

    async def _send_response(
        self,
        code: TuyaBLECode,
        data: bytes,
        response_to: int,
    ) -> None:
        """Send response to received packet while the same session is active."""
        if self.is_connected:
            await self._send_packet_while_connected(code, data, response_to, False)

    async def _send_packet_while_connected(
        self,
        code: TuyaBLECode,
        data: bytes,
        response_to: int,
        wait_for_response: bool,
    ) -> bool:
        """Send packet to device and optionally wait for its response."""
        if wait_for_response:
            async with self._transaction_lock:
                return await self._send_packet_while_connected_locked(
                    code, data, response_to, wait_for_response
                )

        return await self._send_packet_while_connected_locked(
            code, data, response_to, wait_for_response
        )

    async def _send_packet_while_connected_locked(
        self,
        code: TuyaBLECode,
        data: bytes,
        response_to: int,
        wait_for_response: bool,
    ) -> bool:
        """Send one logical packet transaction on the current BLE session."""
        result = True
        future: asyncio.Future[int] | None = None

        if wait_for_response:
            self._clean_input()

        seq_num = await self._get_seq_num()
        if wait_for_response:
            future = asyncio.get_running_loop().create_future()
            self._input_expected_responses[seq_num] = future

        if response_to > 0:
            _LOGGER.debug(
                "%s: Sending packet: #%s %s in response to #%s",
                self.address,
                seq_num,
                code.name,
                response_to,
            )
        else:
            _LOGGER.debug(
                "%s: Sending packet: #%s %s",
                self.address,
                seq_num,
                code.name,
            )

        packets = self._build_packets(seq_num, code, data, response_to)
        try:
            await self._int_send_packet_while_connected(packets)

            if future is not None:
                try:
                    await asyncio.wait_for(future, RESPONSE_WAIT_TIMEOUT)
                except TimeoutError:
                    _LOGGER.debug(
                        "%s: Timeout receiving response to %s; RSSI: %s",
                        self.address,
                        code.name,
                        self.rssi,
                    )
                    result = False
        finally:
            if wait_for_response:
                self._input_expected_responses.pop(seq_num, None)
                if future is not None and future.done() and not future.cancelled():
                    # Retrieve a possible disconnect exception when the GATT write
                    # failed before this coroutine started awaiting the future.
                    try:
                        future.exception()
                    except asyncio.CancelledError:
                        pass
                self._clean_input()

        return result

    async def _int_send_packet_while_connected(
        self,
        packets: list[bytes],
    ) -> None:
        if self._operation_lock.locked():
            _LOGGER.debug(
                "%s: Operation already in progress, waiting for it to complete; "
                "RSSI: %s",
                self.address,
                self.rssi,
            )
        async with self._operation_lock:
            await self._send_packets_locked(packets)

    async def _send_packets_locked(self, packets: list[bytes]) -> None:
        """Write all fragments using the current BLE session only."""
        await self._int_send_packets_locked(packets)

    async def _int_send_packets_locked(self, packets: list[bytes]) -> None:
        """Write fragmented Tuya data with pacing and strict client identity."""
        client = self._client
        if client is None or not client.is_connected:
            self._invalidate_connection(
                "BLE client disconnected before packet send", client
            )
            raise BleakError("BLE client disconnected before packet send")

        for index, packet in enumerate(packets):
            if self._client is not client or not client.is_connected:
                self._invalidate_connection(
                    "BLE client disconnected during packet send", client
                )
                raise BleakError("BLE client disconnected during packet send")

            try:
                await client.write_gatt_char(
                    CHARACTERISTIC_WRITE,
                    packet,
                    False,
                )
            except BLEAK_EXCEPTIONS as err:
                self._invalidate_connection(
                    f"GATT write failed: {err}", client
                )
                raise
            except Exception as err:
                self._invalidate_connection(
                    f"Unexpected GATT write failure: {err}", client
                )
                raise BleakError("Unexpected GATT write failure") from err

            if index + 1 < len(packets):
                await asyncio.sleep(INTER_PACKET_DELAY)

    def _get_key(self, security_flag: int) -> bytes:
        if security_flag == 1:
            return self._auth_key
        if security_flag == 4:
            return self._login_key
        elif security_flag == 5:
            return self._session_key
        else:
            pass

    def _parse_timestamp(self, data: bytes, start_pos: int) -> tuple(float, int):
        timestamp: float
        pos = start_pos
        if pos >= len(data):
            raise TuyaBLEDataLengthError()
        time_type = data[pos]
        pos += 1
        end_pos = pos
        match time_type:
            case 0:
                end_pos += 13
                if end_pos > len(data):
                    raise TuyaBLEDataLengthError()
                timestamp = int(data[pos:end_pos].decode()) / 1000
                pass
            case 1:
                end_pos += 4
                if end_pos > len(data):
                    raise TuyaBLEDataLengthError()
                timestamp = int.from_bytes(data[pos:end_pos], "big") * 1.0
                pass
            case _:
                raise TuyaBLEDataFormatError()

        _LOGGER.debug(
            "%s: Received timestamp: %s",
            self.address,
            time.ctime(timestamp),
        )
        return (timestamp, end_pos)

    def _parse_datapoints_v3(
        self, timestamp: float, flags: int, data: bytes, start_pos: int
    ) -> int:
        datapoints: list[TuyaBLEDataPoint] = []

        pos = start_pos
        while len(data) - pos >= 4:
            id: int = data[pos]
            pos += 1
            _type: int = data[pos]
            if _type > TuyaBLEDataPointType.DT_BITMAP.value:
                raise TuyaBLEDataFormatError()
            type: TuyaBLEDataPointType = TuyaBLEDataPointType(_type)
            pos += 1
            data_len: int = data[pos]
            pos += 1
            next_pos = pos + data_len
            if next_pos > len(data):
                raise TuyaBLEDataLengthError()
            raw_value = data[pos:next_pos]
            match type:
                case (TuyaBLEDataPointType.DT_RAW | TuyaBLEDataPointType.DT_BITMAP):
                    value = raw_value
                case TuyaBLEDataPointType.DT_BOOL:
                    value = int.from_bytes(raw_value, "big") != 0
                case (TuyaBLEDataPointType.DT_VALUE | TuyaBLEDataPointType.DT_ENUM):
                    value = int.from_bytes(raw_value, "big", signed=True)
                case TuyaBLEDataPointType.DT_STRING:
                    value = raw_value.decode()

            _LOGGER.debug(
                "%s: Received datapoint update, id: %s, type: %s: value: %s",
                self.address,
                id,
                type.name,
                value,
            )
            self._datapoints._update_from_device(
                id, timestamp, flags, type, value)
            datapoints.append(self._datapoints[id])
            pos = next_pos

        self._fire_callbacks(datapoints)

    def _handle_command_or_response(
        self, seq_num: int, response_to: int, code: TuyaBLECode, data: bytes
    ) -> None:
        result: int = 0

        match code:
            case TuyaBLECode.FUN_SENDER_DEVICE_INFO:
                if len(data) < 46:
                    raise TuyaBLEDataLengthError()

                self._device_version = ("%s.%s") % (data[0], data[1])
                self._protocol_version_str = ("%s.%s") % (data[2], data[3])
                self._hardware_version = ("%s.%s") % (data[12], data[13])

                self._protocol_version = data[2]
                self._flags = data[4]
                self._is_bound = data[5] != 0

                srand = data[6:12]
                self._session_key = hashlib.md5(
                    self._local_key + srand).digest()
                self._auth_key = data[14:46]

            case TuyaBLECode.FUN_SENDER_PAIR:
                if len(data) != 1:
                    raise TuyaBLEDataLengthError()
                result = data[0]
                if result == 2:
                    _LOGGER.debug(
                        "%s: Device is already paired",
                        self.address,
                    )
                    result = 0
                self._is_paired = result == 0

            case TuyaBLECode.FUN_SENDER_DEVICE_STATUS:
                if len(data) != 1:
                    raise TuyaBLEDataLengthError()
                result = data[0]

            case TuyaBLECode.FUN_RECEIVE_TIME1_REQ:
                if len(data) != 0:
                    raise TuyaBLEDataLengthError()

                timestamp = int(time.time_ns() / 1000000)
                timezone = -int(time.timezone / 36)
                data = str(timestamp).encode() + pack(">h", timezone)
                asyncio.create_task(self._send_response(code, data, seq_num))

            case TuyaBLECode.FUN_RECEIVE_TIME2_REQ:
                if len(data) != 0:
                    raise TuyaBLEDataLengthError()

                time_str: time.struct_time = time.localtime()
                timezone = -int(time.timezone / 36)
                data = pack(
                    ">BBBBBBBh",
                    time_str.tm_year % 100,
                    time_str.tm_mon,
                    time_str.tm_mday,
                    time_str.tm_hour,
                    time_str.tm_min,
                    time_str.tm_sec,
                    time_str.tm_wday,
                    timezone,
                )
                asyncio.create_task(self._send_response(code, data, seq_num))

            case TuyaBLECode.FUN_RECEIVE_DP:
                self._parse_datapoints_v3(time.time(), 0, data, 0)
                asyncio.create_task(
                    self._send_response(code, bytes(0), seq_num))

            case TuyaBLECode.FUN_RECEIVE_SIGN_DP:
                dp_seq_num = int.from_bytes(data[:2], "big")
                flags = data[2]
                self._parse_datapoints_v3(time.time(), flags, data, 2)
                data = pack(">HBB", dp_seq_num, flags, 0)
                asyncio.create_task(self._send_response(code, data, seq_num))

            case TuyaBLECode.FUN_RECEIVE_TIME_DP:
                timestamp: float
                pos: int
                timestamp, pos = self._parse_timestamp(data, 0)
                self._parse_datapoints_v3(timestamp, 0, data, pos)
                asyncio.create_task(
                    self._send_response(code, bytes(0), seq_num))

            case TuyaBLECode.FUN_RECEIVE_SIGN_TIME_DP:
                timestamp: float
                pos: int
                dp_seq_num = int.from_bytes(data[:2], "big")
                flags = data[2]
                timestamp, pos = self._parse_timestamp(data, 3)
                self._parse_datapoints_v3(time.time(), flags, data, pos)
                data = pack(">HBB", dp_seq_num, flags, 0)
                asyncio.create_task(self._send_response(code, data, seq_num))

        if response_to != 0:
            future = self._input_expected_responses.pop(response_to, None)
            if future:
                _LOGGER.debug(
                    "%s: Received expected response to #%s, result: %s",
                    self.address,
                    response_to,
                    result,
                )
                if result == 0:
                    future.set_result(result)
                else:
                    future.set_exception(TuyaBLEDeviceError(result))

    def _clean_input(self) -> None:
        self._input_buffer = None
        self._input_expected_packet_num = 0
        self._input_expected_length = 0

    def _parse_input(self) -> None:
        security_flag = self._input_buffer[0]
        key = self._get_key(security_flag)
        iv = self._input_buffer[1:17]
        encrypted = self._input_buffer[17:]

        self._clean_input()

        cipher = AES.new(key, AES.MODE_CBC, iv)
        raw = cipher.decrypt(encrypted)

        seq_num: int
        response_to: int
        _code: int
        length: int
        seq_num, response_to, _code, length = unpack(">IIHH", raw[:12])

        data_end_pos = length + 12
        raw_length = len(raw)
        if raw_length < data_end_pos:
            raise TuyaBLEDataLengthError()
        if raw_length > data_end_pos:
            calc_crc = self._calc_crc16(raw[:data_end_pos])
            (data_crc,) = unpack(
                ">H",
                raw[data_end_pos:data_end_pos + 2]  # fmt: skip
            )
            if calc_crc != data_crc:
                raise TuyaBLEDataCRCError()
        data = raw[12:data_end_pos]

        code: TuyaBLECode
        try:
            code = TuyaBLECode(_code)
        except ValueError:
            _LOGGER.debug(
                "%s: Received unknown message: #%s %x, response to #%s, data %s",
                self.address,
                seq_num,
                _code,
                response_to,
                data.hex(),
            )
            return

        if response_to != 0:
            _LOGGER.debug(
                "%s: Received: #%s %s, response to #%s",
                self.address,
                seq_num,
                code.name,
                response_to,
            )
        else:
            _LOGGER.debug(
                "%s: Received: #%s %s",
                self.address,
                seq_num,
                code.name,
            )

        self._handle_command_or_response(seq_num, response_to, code, data)

    def _notification_handler(self, _sender: int, data: bytearray) -> None:
        """Handle notification responses."""
        _LOGGER.debug("%s: Packet received: %s", self.address, data.hex())

        try:
            pos: int = 0
            packet_num, pos = self._unpack_int(data, pos)

            if packet_num == 0:
                # A packet with number 0 always starts a new BLE notification frame.
                # This prevents stale/partial frames from poisoning the next response.
                self._input_buffer = bytearray()
                self._input_expected_length, pos = self._unpack_int(data, pos)
                pos += 1
                self._input_buffer += data[pos:]
                self._input_expected_packet_num = 1
            elif (
                self._input_buffer is not None
                and packet_num == self._input_expected_packet_num
            ):
                self._input_buffer += data[pos:]
                self._input_expected_packet_num += 1
            else:
                _LOGGER.debug(
                    "%s: Ignoring out-of-order notification packet %s, expected %s",
                    self.address,
                    packet_num,
                    self._input_expected_packet_num,
                )
                if packet_num > self._input_expected_packet_num:
                    self._clean_input()
                return

            if self._input_buffer is None:
                return

            if len(self._input_buffer) > self._input_expected_length:
                _LOGGER.error(
                    "%s: Unexpected length of data in notifications, "
                    "received %s expected %s",
                    self.address,
                    len(self._input_buffer),
                    self._input_expected_length,
                )
                self._clean_input()
                return

            if len(self._input_buffer) == self._input_expected_length:
                self._parse_input()
        except Exception:
            _LOGGER.error("%s: Error handling notification", self.address, exc_info=True)
            self._clean_input()

    async def _send_datapoints_v3(self, datapoint_ids: list[int]) -> None:
        """Send new values of datapoints to the device."""
        data = bytearray()
        for dp_id in datapoint_ids:
            dp = self._datapoints[dp_id]
            value = dp._get_value()
            _LOGGER.debug(
                "%s: Sending datapoint update, id: %s, type: %s: value: %s",
                self.address,
                dp.id,
                dp.type.name,
                dp.value,
            )
            data += pack(">BBB", dp.id, int(dp.type.value), len(value))
            data += value

        #await self._send_packet(TuyaBLECode.FUN_SENDER_DPS, data)
        await self._send_packet(TuyaBLECode.FUN_SENDER_DPS, data, False)

    async def _send_datapoints(self, datapoint_ids: list[int]) -> None:
        """Send new values of datapoints to the device."""
        if self._protocol_version == 3:
            await self._send_datapoints_v3(datapoint_ids)
        else:
            raise TuyaBLEDeviceError(0)
