"""Device helpers for Tuya Local BLE Lock."""
from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo, EntityDescription
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from home_assistant_bluetooth import BluetoothServiceInfoBleak

from .const import DEVICE_DEF_MANUFACTURER, DOMAIN, SET_DISCONNECTED_DELAY
from .keyman import HASSTuyaBLEDeviceManager
from .tuya_ble import (
    AbstaractTuyaBLEDeviceManager,
    TuyaBLEDataPoint,
    TuyaBLEDevice,
    TuyaBLEDeviceCredentials,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class TuyaBLEProductInfo:
    name: str
    manufacturer: str = DEVICE_DEF_MANUFACTURER


@dataclass
class TuyaBLECategoryInfo:
    products: dict[str, TuyaBLEProductInfo]
    info: TuyaBLEProductInfo | None = None


# Оставлен только ваш замок A1 PRO MAX: category=jtmspro, product_id=rlyxv7pe.
devices_database: dict[str, TuyaBLECategoryInfo] = {
    "jtmspro": TuyaBLECategoryInfo(
        products={
            "rlyxv7pe": TuyaBLEProductInfo(name="A1 PRO MAX"),
        },
    ),
}


def get_product_info_by_ids(category: str, product_id: str) -> TuyaBLEProductInfo | None:
    category_info = devices_database.get(category)
    if category_info is None:
        return None
    return category_info.products.get(product_id) or category_info.info


def get_device_product_info(device: TuyaBLEDevice) -> TuyaBLEProductInfo | None:
    return get_product_info_by_ids(device.category, device.product_id)


def get_short_address(address: str) -> str:
    parts = address.replace("-", ":").upper().split(":")
    return f"{parts[-3]}{parts[-2]}{parts[-1]}"[-6:]


async def get_device_readable_name(
    discovery_info: BluetoothServiceInfoBleak,
    manager: AbstaractTuyaBLEDeviceManager | None,
) -> str:
    credentials: TuyaBLEDeviceCredentials | None = None
    product_info: TuyaBLEProductInfo | None = None

    if manager:
        credentials = await manager.get_device_credentials(discovery_info.address)
        if credentials:
            product_info = get_product_info_by_ids(credentials.category, credentials.product_id)

    short_address = get_short_address(discovery_info.address)
    if product_info:
        return f"{product_info.name} {short_address}"
    if credentials and credentials.device_name:
        return f"{credentials.device_name} {short_address}"
    return f"{discovery_info.device.name or discovery_info.address} {short_address}"


def get_device_info(device: TuyaBLEDevice) -> DeviceInfo:
    product_info = get_device_product_info(device)
    product_name = product_info.name if product_info else device.name

    return DeviceInfo(
        connections={(dr.CONNECTION_BLUETOOTH, device.address)},
        identifiers={(DOMAIN, device.address)},
        manufacturer=product_info.manufacturer if product_info else DEVICE_DEF_MANUFACTURER,
        model=f"{device.product_model or product_name} ({device.product_id})",
        name=f"{product_name} {get_short_address(device.address)}",
        hw_version=device.hardware_version,
        sw_version=f"{device.device_version} (protocol {device.protocol_version})",
    )


class TuyaBLEEntity(CoordinatorEntity):
    """Base entity for Tuya BLE lock."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: "TuyaBLECoordinator",
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo | None,
        description: EntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self._hass = hass
        self._device = device
        self._product = product
        self.entity_description = description
        self._attr_has_entity_name = True
        self._attr_device_info = get_device_info(device)
        self._attr_unique_id = f"{device.device_id}-{description.key}"
        if description.translation_key is None:
            self._attr_translation_key = description.key

    @property
    def available(self) -> bool:
        return self.coordinator.connected


class TuyaBLECoordinator(DataUpdateCoordinator[None]):
    """Coordinator for BLE push updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        device: TuyaBLEDevice,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=config_entry)
        self._device = device
        self._disconnected = True
        self._unsub_disconnect: CALLBACK_TYPE | None = None
        device.register_connected_callback(self._async_handle_connect)
        device.register_callback(self._async_handle_update)
        device.register_disconnected_callback(self._async_handle_disconnect)

    @property
    def connected(self) -> bool:
        return not self._disconnected

    @callback
    def _async_handle_connect(self) -> None:
        if self._unsub_disconnect is not None:
            self._unsub_disconnect()
            self._unsub_disconnect = None
        if self._disconnected:
            self._disconnected = False
            self.async_update_listeners()

    @callback
    def _async_handle_update(self, _updates: list[TuyaBLEDataPoint]) -> None:
        self._async_handle_connect()
        self.async_set_updated_data(None)

    @callback
    def _set_disconnected(self, _now: object | None = None) -> None:
        self._disconnected = True
        self._unsub_disconnect = None
        self.async_update_listeners()

    @callback
    def _async_handle_disconnect(self) -> None:
        if self._unsub_disconnect is None:
            self._unsub_disconnect = async_call_later(
                self.hass,
                SET_DISCONNECTED_DELAY,
                self._set_disconnected,
            )


@dataclass
class TuyaBLEData:
    title: str
    device: TuyaBLEDevice
    product: TuyaBLEProductInfo | None
    manager: HASSTuyaBLEDeviceManager
    coordinator: TuyaBLECoordinator
