"""Sensor platform for Tuya Local BLE."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .devices import TuyaBLEData, get_device_info
from .tuya_ble import TuyaBLEDataPointType


BATTERY_STATE_DP_ID = 9

BATTERY_STATE_OPTIONS: list[str] = [
    "High",
    "Normal",
    "Low",
    "Critical",
]

BATTERY_STATE_ICONS: list[str] = [
    "mdi:battery-check",
    "mdi:battery-50",
    "mdi:battery-alert",
    "mdi:battery-alert",
]


BATTERY_STATE_DESCRIPTION = SensorEntityDescription(
    key="battery_state",
    name="Battery state",
    icon="mdi:battery",
    device_class=SensorDeviceClass.ENUM,
    options=BATTERY_STATE_OPTIONS,
    entity_category=EntityCategory.DIAGNOSTIC,
)

SIGNAL_STRENGTH_DESCRIPTION = SensorEntityDescription(
    key="signal_strength",
    name="Signal strength",
    device_class=SensorDeviceClass.SIGNAL_STRENGTH,
    native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tuya Local BLE sensors."""
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            TuyaBLEBatteryStateSensor(data),
            TuyaBLESimpleSensor(data, SIGNAL_STRENGTH_DESCRIPTION),
        ]
    )


class TuyaBLESimpleSensor(CoordinatorEntity, SensorEntity):
    """Simple diagnostic sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        data: TuyaBLEData,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(data.coordinator)
        self._data = data
        self.entity_description = description
        self._attr_unique_id = f"{data.device.device_id}-{description.key}"
        self._attr_device_info = get_device_info(data.device)

    @property
    def native_value(self) -> Any:
        """Return native value."""
        if self.entity_description.key == "signal_strength":
            return self._data.device.rssi
        return None

    @property
    def available(self) -> bool:
        """Return availability."""
        return True


class TuyaBLEBatteryStateSensor(CoordinatorEntity, SensorEntity):
    """Battery state sensor."""

    _attr_has_entity_name = True
    entity_description = BATTERY_STATE_DESCRIPTION

    def __init__(self, data: TuyaBLEData) -> None:
        super().__init__(data.coordinator)
        self._data = data
        self._attr_unique_id = f"{data.device.device_id}-battery_state"
        self._attr_device_info = get_device_info(data.device)

        data.device.datapoints.get_or_create(
            BATTERY_STATE_DP_ID,
            TuyaBLEDataPointType.DT_ENUM,
            0,
        )

    @property
    def native_value(self) -> str | int | None:
        """Return battery state."""
        dp = self._data.device.datapoints[BATTERY_STATE_DP_ID]

        if dp is None:
            return None

        value = dp.value

        if isinstance(value, bytes):
            value = int.from_bytes(value, "big")

        if isinstance(value, bool):
            value = int(value)

        if isinstance(value, int):
            if 0 <= value < len(BATTERY_STATE_OPTIONS):
                return BATTERY_STATE_OPTIONS[value]
            return value

        return str(value)

    @property
    def icon(self) -> str | None:
        """Return battery icon."""
        dp = self._data.device.datapoints[BATTERY_STATE_DP_ID]

        if dp is None:
            return "mdi:battery"

        value = dp.value

        if isinstance(value, bytes):
            value = int.from_bytes(value, "big")

        if isinstance(value, bool):
            value = int(value)

        if isinstance(value, int) and 0 <= value < len(BATTERY_STATE_ICONS):
            return BATTERY_STATE_ICONS[value]

        return "mdi:battery"

    @property
    def available(self) -> bool:
        """Return availability."""
        return True