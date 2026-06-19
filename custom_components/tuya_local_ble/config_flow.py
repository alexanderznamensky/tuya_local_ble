"""Config flow for Tuya Local BLE Lock."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN, SERVICE_UUID
from .devices import get_device_readable_name
from .keyman import HASSTuyaBLEDeviceManager

_LOGGER = logging.getLogger(__name__)


class TuyaBLEConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tuya Local BLE Lock."""

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}
        self._data: dict[str, Any] = {}
        self._manager: HASSTuyaBLEDeviceManager | None = None

    async def async_step_bluetooth(
        self,
        discovery_info: BluetoothServiceInfoBleak,
    ) -> FlowResult:
        """Handle Bluetooth discovery."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        self._manager = HASSTuyaBLEDeviceManager(self.hass, self._data)
        await self._manager.async_load_devices_file()
        self.context["title_placeholders"] = {
            "name": await get_device_readable_name(discovery_info, self._manager)
        }
        return await self.async_step_user()

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Handle manual setup from devices.json or discovered BLE devices."""
        errors: dict[str, str] = {}

        if self._manager is None:
            self._manager = HASSTuyaBLEDeviceManager(self.hass, self._data)
            await self._manager.async_load_devices_file()

        if user_input is not None:
            address = user_input[CONF_ADDRESS].upper()
            credentials = await self._manager.get_device_credentials(address)

            if credentials is None:
                errors["base"] = "device_not_registered"
            else:
                await self.async_set_unique_id(address, raise_on_progress=False)
                self._abort_if_unique_id_configured()

                title = (
                    credentials.device_name
                    or credentials.product_model
                    or credentials.product_name
                    or address
                )
                return self.async_create_entry(
                    title=title,
                    data={CONF_ADDRESS: address},
                    options=self._data,
                )

        choices = await self._get_device_choices()

        if not choices:
            return self.async_abort(reason="no_unconfigured_devices")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ADDRESS): vol.In(choices)}),
            errors=errors,
        )

    async def _get_device_choices(self) -> dict[str, str]:
        """Return selectable devices from devices.json and Bluetooth discovery."""
        assert self._manager is not None

        current_addresses = {address.upper() for address in self._async_current_ids()}
        choices: dict[str, str] = {}

        # Primary source: config/tuya_local_ble/devices.json.
        # This allows setup even when the lock is not advertising at the moment.
        for address, item in self._manager.devices.items():
            address = address.upper()
            if address in current_addresses:
                continue
            name = (
                item.get("device_name")
                or item.get("product_model")
                or item.get("product_name")
                or address
            )
            choices[address] = f"{name} ({address})"

        # Optional source: currently discovered Tuya BLE devices.
        if self._discovery_info is not None:
            self._discovered_devices[self._discovery_info.address.upper()] = self._discovery_info

        for discovery in async_discovered_service_info(self.hass):
            address = discovery.address.upper()
            if address in current_addresses or address in self._discovered_devices:
                continue
            if SERVICE_UUID not in discovery.service_data:
                continue
            self._discovered_devices[address] = discovery

        for address, discovery in self._discovered_devices.items():
            address = address.upper()
            if address in current_addresses or address in choices:
                continue
            choices[address] = await get_device_readable_name(discovery, self._manager)

        return choices
