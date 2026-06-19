"""Tuya Local BLE integration."""
from __future__ import annotations

import logging

from bleak.backends.device import BLEDevice

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.match import ADDRESS, BluetoothCallbackMatcher
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN
from .devices import TuyaBLECoordinator, TuyaBLEData, get_device_product_info
from .keyman import HASSTuyaBLEDeviceManager
from .tuya_ble import TuyaBLEDevice

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LOCK,
    Platform.SENSOR,
    Platform.SELECT,
]


def _make_placeholder_ble_device(address: str, name: str | None = None) -> BLEDevice:
    """Create a placeholder BLE device so the config entry can load while the lock sleeps."""
    return BLEDevice(address=address, name=name or address, details={}, rssi=0)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Tuya Local BLE lock from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    normalized_address = address.upper()

    manager = HASSTuyaBLEDeviceManager(hass, entry.options.copy())
    await manager.async_load_devices_file()

    credentials = await manager.get_device_credentials(address)
    placeholder_name = (
        credentials.device_name
        if credentials and credentials.device_name
        else entry.title
    )

    ble_device = bluetooth.async_ble_device_from_address(
        hass,
        normalized_address,
        True,
    )

    device_was_found = ble_device is not None

    if ble_device is None:
        _LOGGER.warning(
            "%s: BLE device not found during setup. Entry will load as unavailable and wait for Bluetooth discovery.",
            address,
        )
        ble_device = _make_placeholder_ble_device(address, placeholder_name)

    device = TuyaBLEDevice(manager, ble_device)
    await device.initialize()

    product_info = get_device_product_info(device)
    coordinator = TuyaBLECoordinator(hass, device, entry)

    async def _async_try_device_update(reason: str) -> None:
        try:
            await device.update()
        except Exception:
            _LOGGER.debug(
                "%s: device update failed after %s",
                address,
                reason,
                exc_info=True,
            )

    @callback
    def _async_update_ble(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Update the BLE device reference when Home Assistant sees the sleeping lock."""
        _LOGGER.debug(
            "%s: Bluetooth advertisement received; refreshing BLE device reference",
            address,
        )

        device.set_ble_device_and_advertisement_data(
            service_info.device,
            service_info.advertisement,
        )

        hass.async_create_task(
            _async_try_device_update("bluetooth discovery")
        )

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_update_ble,
            BluetoothCallbackMatcher({ADDRESS: normalized_address}),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = TuyaBLEData(
        entry.title,
        device,
        product_info,
        manager,
        coordinator,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(
        entry.add_update_listener(_async_update_listener)
    )

    if device_was_found:
        hass.async_create_task(
            _async_try_device_update("initial setup")
        )

    async def _async_stop(event: Event) -> None:
        await device.stop()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry update."""
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]

    if entry.title != data.title:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        data: TuyaBLEData = hass.data[DOMAIN].pop(entry.entry_id)
        await data.device.stop()

    return unload_ok