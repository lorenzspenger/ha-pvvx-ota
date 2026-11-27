"""Repair flows: bring back the settings after "reset to defaults"."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .store import ConfigStore


class RestoreSettingsRepairFlow(RepairsFlow):
    """Same restore as the "Restore previous settings" button, started from the repair issue."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    def _store(self) -> ConfigStore | None:
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            return None
        store: ConfigStore = entry.runtime_data
        return store

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, str] | None = None) -> RepairsFlowResult:
        store = self._store()
        if store is None or not store.can_restore:
            return self.async_abort(reason="cannot_restore")
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                await store.async_restore()
            except HomeAssistantError:
                errors["base"] = "restore_failed"
            else:
                remaining = ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id)
                if remaining is None or not remaining.active:
                    return self.async_create_entry(data={})
                errors["base"] = "restore_failed"  # written, but the sensor still differs from the saved settings
        schema: Any = vol.Schema({})  # voluptuous, which Home Assistant still accepts
        return self.async_show_form(
            step_id="confirm", data_schema=schema, errors=errors, description_placeholders={"name": store.name}
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """Only reset_defaults is fixable."""
    return RestoreSettingsRepairFlow(str((data or {}).get("entry_id", "")))
