"""Local credentials manager for Tuya BLE devices."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from homeassistant.const import CONF_DEVICE_ID
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CATEGORY,
    CONF_CRED_FILE,
    CONF_DEVICE_NAME,
    CONF_LOCAL_KEY,
    CONF_PRODUCT_ID,
    CONF_PRODUCT_MODEL,
    CONF_PRODUCT_NAME,
    CONF_UUID,
)
from .tuya_ble import AbstaractTuyaBLEDeviceManager, TuyaBLEDeviceCredentials

_LOGGER = logging.getLogger(__name__)


class HASSTuyaBLEDeviceManager(AbstaractTuyaBLEDeviceManager):
    """Reads Tuya BLE credentials from config/tuya_local_ble/devices.json."""

    def __init__(self, hass: HomeAssistant, data: dict[str, Any] | None = None) -> None:
        self._hass = hass
        self._data = data or {}
        self._devicedata: dict[str, dict[str, Any]] = {}
        self._loaded = False

    def _load_devices_file_sync(self) -> dict[str, dict[str, Any]]:
        """Load credentials file in a sync helper intended for executor use."""
        path = os.path.join(self._hass.config.config_dir, CONF_CRED_FILE)
        try:
            with open(path, encoding="utf-8") as file:
                raw = json.load(file)
        except FileNotFoundError:
            _LOGGER.error("Tuya BLE credentials file not found: %s", path)
            raw = {}
        except json.JSONDecodeError:
            _LOGGER.exception("Tuya BLE credentials file has invalid JSON: %s", path)
            raw = {}

        if not isinstance(raw, dict):
            _LOGGER.error("Tuya BLE credentials file must contain a JSON object: %s", path)
            return {}

        return {str(key).upper(): value for key, value in raw.items() if isinstance(value, dict)}

    async def async_load_devices_file(self, force: bool = False) -> None:
        """Load devices.json without blocking the Home Assistant event loop."""
        if self._loaded and not force:
            return
        self._devicedata = await self._hass.async_add_executor_job(
            self._load_devices_file_sync
        )
        self._loaded = True

    async def get_device_credentials(
        self,
        address: str,
        force_update: bool = False,
        save_data: bool = False,
    ) -> TuyaBLEDeviceCredentials | None:
        if force_update or not self._loaded:
            await self.async_load_devices_file(force=force_update)

        credentials = self._devicedata.get(address.upper())
        if credentials is None:
            return None

        return TuyaBLEDeviceCredentials(
            credentials.get(CONF_UUID, ""),
            credentials.get(CONF_LOCAL_KEY, ""),
            credentials.get(CONF_DEVICE_ID, ""),
            credentials.get(CONF_CATEGORY, ""),
            credentials.get(CONF_PRODUCT_ID, ""),
            credentials.get(CONF_DEVICE_NAME, ""),
            credentials.get(CONF_PRODUCT_MODEL, ""),
            credentials.get(CONF_PRODUCT_NAME, ""),
        )

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    @property
    def devices(self) -> dict[str, dict[str, Any]]:
        """Return devices loaded from devices.json, keyed by BLE address."""
        return self._devicedata
