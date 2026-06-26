"""Lock platform for Tuya Local BLE."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any, Callable

from homeassistant.components.lock import LockEntity, LockEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .devices import TuyaBLEData, TuyaBLEEntity, TuyaBLEProductInfo
from .tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice

_LOGGER = logging.getLogger(__name__)

TuyaBLELockIsAvailable = Callable[["TuyaBLELock", TuyaBLEProductInfo | None], bool] | None


@dataclass
class TuyaBLELockMapping:
    dp_id: int
    dp_id_lock: int
    dp_id_unlock: int
    dp_id_nop: int
    keep_connect_timer: int
    description: LockEntityDescription = field(
        default_factory=lambda: LockEntityDescription(key="manual_lock")
    )
    force_add: bool = True
    keep_connect: bool = False
    dp_type: TuyaBLEDataPointType | None = None
    is_available: TuyaBLELockIsAvailable = None


@dataclass
class TuyaBLECategoryLockMapping:
    products: dict[str, list[TuyaBLELockMapping]] | None = None
    mapping: list[TuyaBLELockMapping] | None = None


# Оставлен только A1 PRO MAX / Gimdow Smart Lock.
mapping: dict[str, TuyaBLECategoryLockMapping] = {
    "jtmspro": TuyaBLECategoryLockMapping(
        products={
            "rlyxv7pe": [
                TuyaBLELockMapping(
                    dp_id_unlock=6,
                    dp_id_lock=46,
                    dp_id=47,
                    dp_id_nop=52,
                    keep_connect=True,
                    keep_connect_timer=60,
                    description=LockEntityDescription(key="manual_lock"),
                ),
            ],
        },
    ),
}


def get_mapping_by_device(device: TuyaBLEDevice) -> list[TuyaBLELockMapping]:
    category = mapping.get(device.category)
    if category is None:
        return []
    if category.products is not None:
        product_mapping = category.products.get(device.product_id)
        if product_mapping is not None:
            return product_mapping
    return category.mapping or []


class TuyaBLELock(TuyaBLEEntity, LockEntity):
    """Tuya BLE lock entity."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo | None,
        mapping: TuyaBLELockMapping,
    ) -> None:
        super().__init__(hass, coordinator, device, product, mapping.description)
        self._mapping = mapping
        self._current_state: bool | None = None
        self._target_state: bool | None = None
        self._commanded = False
        self._commanded_timer: datetime | None = None
        self._datapoint_nop = None
        self._is_jammed = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if not self._mapping.keep_connect:
            return

        self._datapoint_nop = self._device.datapoints.get_or_create(
            self._mapping.dp_id_nop,
            TuyaBLEDataPointType.DT_BOOL,
            False,
        )

        async def _async_send_keep_alive() -> None:
            # Спящие BLE-замки часто недоступны между рекламными пакетами.
            # Не отправляем NOP, пока устройство не подключено/не paired, иначе
            # Home Assistant получает "Task exception was never retrieved".
            if not self.coordinator.connected or not self._datapoint_nop:
                return
            try:
                await self._datapoint_nop.set_value(True)
            except Exception:
                _LOGGER.debug(
                    "%s: keep-alive datapoint send failed",
                    self._device.address,
                    exc_info=True,
                )

        @callback
        def _send_keep_alive(_now) -> None:
            self.hass.async_create_task(_async_send_keep_alive())

        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                _send_keep_alive,
                timedelta(seconds=self._mapping.keep_connect_timer),
            )
        )

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def is_locked(self) -> bool | None:
        return self._current_state

    @property
    def is_locking(self) -> bool:
        return self._current_state is False and self._target_state is True and self._commanded

    @property
    def is_unlocking(self) -> bool:
        return self._current_state is True and self._target_state is False and self._commanded

    @property
    def is_jammed(self) -> bool:
        return self._is_jammed

    async def async_lock(self, **kwargs: Any) -> None:
        await self._set_lock_state(True)

    async def async_unlock(self, **kwargs: Any) -> None:
        await self._set_lock_state(False)

    async def _set_lock_state(self, want_locked: bool) -> None:
        """Send lock/unlock command.

        BLE locks usually advertise while sleeping and are not kept connected.
        Do not fail only because the coordinator is currently disconnected:
        datapoint.set_value() goes through TuyaBLEDevice._send_packet(), which
        establishes the BLE connection and pairs before sending the command.
        """
        self._target_state = want_locked
        self._commanded = True
        self._is_jammed = False
        self._commanded_timer = datetime.now()
        self.async_write_ha_state()

        dp_id = self._mapping.dp_id_lock if want_locked else self._mapping.dp_id_unlock
        datapoint = self._device.datapoints.get_or_create(
            dp_id,
            TuyaBLEDataPointType.DT_BOOL,
            False,
        )

        try:
            await datapoint.set_value(True)
        except Exception as err:
            self._commanded = False
            self._target_state = None
            self._commanded_timer = None
            self.async_write_ha_state()
            raise HomeAssistantError(
                f"{self._device.address}: failed to connect to BLE lock and send command"
            ) from err

    def _update_device_state(self) -> None:
        datapoint = self._device.datapoints[self._mapping.dp_id]
        if datapoint is None:
            return

        # Для этого замка исходная логика такая: DP47 True = unlocked, False = locked.
        self._current_state = False if bool(datapoint.value) else True

        if not self._commanded:
            return

        if self._target_state is not None and self._current_state == self._target_state:
            self._commanded = False
            self._is_jammed = False
            return

        if self._commanded_timer and datetime.now() > self._commanded_timer + timedelta(seconds=12):
            self._commanded = False
            self._is_jammed = True

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_device_state()
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        # A sleeping BLE lock is normally not connected between commands.
        # Treat the entity as available once the config entry is loaded;
        # the command itself will try to establish the BLE connection.
        if self._mapping.is_available:
            return self._mapping.is_available(self, self._product)
        return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tuya BLE lock."""
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    entities = [
        TuyaBLELock(hass, data.coordinator, data.device, data.product, item)
        for item in get_mapping_by_device(data.device)
        if item.force_add or data.device.datapoints.has_id(item.dp_id, item.dp_type)
    ]
    async_add_entities(entities)
