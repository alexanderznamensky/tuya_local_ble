"""The Tuya BLE integration."""
from __future__ import annotations

from dataclasses import dataclass, field

import logging
from typing import Callable, Any, Optional
from datetime import datetime, timedelta
from threading import Timer
import time

from homeassistant.components.lock import (
    LockEntityDescription,
    LockEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .devices import TuyaBLEData, TuyaBLEEntity, TuyaBLEProductInfo
from .tuya_ble import TuyaBLEDataPointType, TuyaBLEDevice

_LOGGER = logging.getLogger(__name__)

TuyaBLELockIsAvailable = Callable[["TuyaBLELock", TuyaBLEProductInfo], bool] | None


@dataclass
class TuyaBLELockMapping:
    dp_id: int
    dp_id_lock: int
    dp_id_unlock: int
    dp_id_nop: int
    keep_connect_timer: int
    description: LockEntityDescription
    force_add: bool = True
    keep_connect: bool = False
    dp_type: TuyaBLEDataPointType | None = None
    is_available: TuyaBLELockIsAvailable = None


# Сохраняем «вторую» декларацию, как в исходниках, — она лишь задаёт значения по умолчанию
@dataclass
class TuyaBLELockMapping(TuyaBLELockMapping):
    description: LockEntityDescription = field(
        default_factory=lambda: LockEntityDescription(
            key="push",
            translation_key="push",
        )
    )
    is_available: TuyaBLELockIsAvailable = 0  # type: ignore[assignment]


@dataclass
class TuyaBLECategoryLockMapping:
    products: dict[str, list[TuyaBLELockMapping]] | None = None
    mapping: list[TuyaBLELockMapping] | None = None


mapping: dict[str, TuyaBLECategoryLockMapping] = {
    "jtmspro": TuyaBLECategoryLockMapping(
        products={
            "rlyxv7pe":  # Gimdow Smart Lock
            [
                TuyaBLELockMapping(
                    dp_id_unlock=6,
                    dp_id_lock=46,
                    dp_id=47,
                    # refer to sdk, dp 52 is for deleting temp password
                    # should be safe as a dummy keep alive message
                    dp_id_nop=52,
                    keep_connect=True,
                    keep_connect_timer=60,
                    description=LockEntityDescription(key="manual_lock"),
                ),
            ]
        }
    ),
}


def get_mapping_by_device(device: TuyaBLEDevice) -> list[TuyaBLECategoryLockMapping]:
    category = mapping.get(device.category)
    if category is not None and category.products is not None:
        product_mapping = category.products.get(device.product_id)
        if product_mapping is not None:
            return product_mapping
        if category.mapping is not None:
            return category.mapping
        else:
            return []
    else:
        return []


class TuyaBLELock(TuyaBLEEntity, LockEntity):
    """Representation of a Tuya BLE Lock."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator,
        device: TuyaBLEDevice,
        product: TuyaBLEProductInfo,
        mapping: TuyaBLELockMapping,
    ) -> None:
        super().__init__(hass, coordinator, device, product, mapping.description)
        self._mapping = mapping

        # Состояние замка:
        #   True  -> locked
        #   False -> unlocked
        #   None  -> unknown
        self._current_state: Optional[bool] = None
        self._target_state: Optional[bool] = None

        self._commanded = False
        self._commanded_timer: Optional[datetime] = None
        self._datapoint_nop = None
        self._isjammed = False

        self._update_attrs()

        if mapping.keep_connect:
            self._thread = Timer(self._mapping.keep_connect_timer, self.send_nop_request)
            self._thread.start()
            self._datapoint_nop = device.datapoints.get_or_create(
                self._mapping.dp_id_nop,
                TuyaBLEDataPointType.DT_BOOL,
                False,
            )

    def send_nop_request(self):
        while True:
            if self._datapoint_nop:
                self._hass.create_task(self._datapoint_nop.set_value(True))
            time.sleep(self._mapping.keep_connect_timer)

    # ==== Стандартные свойства LockEntity (формируют LockState) ====

    @property
    def is_locked(self) -> bool | None:
        """Return true if device is locked."""
        return self._current_state  # True/False/None как ожидает LockEntity

    @property
    def is_locking(self) -> bool | None:
        """Return true if device is locking."""
        return (
            self._current_state is False
            and self._target_state is True
            and self._commanded
        )

    @property
    def is_unlocking(self) -> bool | None:
        """Return true if device is unlocking."""
        return (
            self._current_state is True
            and self._target_state is False
            and self._commanded
        )

    @property
    def is_jammed(self) -> bool | None:
        """Return true if device is jammed."""
        return self._isjammed

    # Alarm properties
    @property
    def should_poll(self) -> bool:
        return False

    def _update_attrs(self) -> None:
        # Эти атрибуты LockEntity читает напрямую, но можно и через properties
        self._attr_is_locking = self.is_locking
        self._attr_is_unlocking = self.is_unlocking
        self._attr_is_locked = self.is_locked
        self._attr_is_jammed = self.is_jammed
        self._attr_changed_by = super().changed_by

    # ==== Команды ====

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the device."""
        await self._set_lock_state(True)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the device."""
        await self._set_lock_state(False)

    async def _set_lock_state(self, want_locked: bool) -> None:
        self._target_state = want_locked
        self._update_attrs()
        self.async_write_ha_state()

        dp_id = self._mapping.dp_id_lock if want_locked else self._mapping.dp_id_unlock

        datapoint = self._device.datapoints.get_or_create(
            dp_id,
            TuyaBLEDataPointType.DT_BOOL,
            False,
        )

        # Gimdow require True to activate lock/unlock commands
        self._hass.create_task(datapoint.set_value(True))
        self._commanded = True
        self._commanded_timer = datetime.now()

    # ==== Обновление состояния с устройства ====

    def update_device_state(self):
        datapoint = self._device.datapoints[self._mapping.dp_id]
        if datapoint:
            # По исходной логике: True -> UNLOCKED, False -> LOCKED
            self._current_state = False if datapoint.value else True

            if self._commanded:
                if self._target_state is not None and self._current_state != self._target_state:
                    # Если целевое состояние не достигнуто за разумное время — джэм
                    if self._commanded_timer and (
                        datetime.now() > self._commanded_timer + timedelta(seconds=12)
                    ):
                        self._isjammed = True
                        self._commanded = False
                else:
                    self._commanded = False
                    self._isjammed = False

    @callback
    def _handle_coordinator_update(self) -> None:
        self.update_device_state()
        self._update_attrs()
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        result = super().available
        if result and self._mapping.is_available:
            result = self._mapping.is_available(self, self._product)
        return result


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Tuya BLE locks."""
    data: TuyaBLEData = hass.data[DOMAIN][entry.entry_id]
    mappings = get_mapping_by_device(data.device)
    entities: list[TuyaBLELock] = []
    for mapping in mappings:
        if mapping.force_add or data.device.datapoints.has_id(
            mapping.dp_id, mapping.dp_type
        ):
            entities.append(
                TuyaBLELock(
                    hass,
                    data.coordinator,
                    data.device,
                    data.product,
                    mapping,
                )
            )
    async_add_entities(entities)
