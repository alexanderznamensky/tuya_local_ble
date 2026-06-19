"""Select platform for Tuya Local BLE."""
from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .devices import TuyaBLEData, get_device_info
from .tuya_ble import TuyaBLEDataPointType


LOCK_VOLUME_DP_ID = 31

LOCK_VOLUME_OPTIONS: list[str] = [
    "mute",
    "low",
    "normal",
    "high",
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tuya Local BLE select entities."""
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            TuyaBLELockVolumeSelect(data),
        ]
    )


class TuyaBLELockVolumeSelect(CoordinatorEntity, SelectEntity):
    """Lock volume select."""

    _attr_has_entity_name = True
    _attr_name = "Lock Volume"
    _attr_icon = "mdi:volume-high"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = LOCK_VOLUME_OPTIONS

    def __init__(self, data: TuyaBLEData) -> None:
        super().__init__(data.coordinator)
        self._data = data
        self._attr_unique_id = f"{data.device.device_id}-beep_volume"
        self._attr_device_info = get_device_info(data.device)

        data.device.datapoints.get_or_create(
            LOCK_VOLUME_DP_ID,
            TuyaBLEDataPointType.DT_ENUM,
            0,
        )

    @property
    def current_option(self) -> str | None:
        """Return current selected option."""
        datapoint = self._data.device.datapoints[LOCK_VOLUME_DP_ID]

        if datapoint is None:
            return None

        value: Any = datapoint.value

        if isinstance(value, bytes):
            value = int.from_bytes(value, "big")

        if isinstance(value, bool):
            value = int(value)

        if isinstance(value, int):
            if 0 <= value < len(LOCK_VOLUME_OPTIONS):
                return LOCK_VOLUME_OPTIONS[value]
            return str(value)

        return str(value)

    async def async_select_option(self, option: str) -> None:
        """Change lock volume."""
        if option not in LOCK_VOLUME_OPTIONS:
            return

        int_value = LOCK_VOLUME_OPTIONS.index(option)

        datapoint = self._data.device.datapoints.get_or_create(
            LOCK_VOLUME_DP_ID,
            TuyaBLEDataPointType.DT_ENUM,
            int_value,
        )

        await datapoint.set_value(int_value)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return availability."""
        return True