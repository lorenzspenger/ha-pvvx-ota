"""Diagnostic sensor: hardware version byte of the config block (read only)."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import config as cfgmod
from .entity import PvvxConfigEntity, get_store, wanted

PARALLEL_UPDATES = 1  # one Bluetooth operation at a time, like the shared lock


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    store = get_store(hass, entry)
    if wanted(store, "adv_interval"):
        add([HwVerSensor(hass, entry)])


class HwVerSensor(PvvxConfigEntity, SensorEntity):
    """hw_ver is read only in the firmware (it is copied from the device on every write), so no setter exists."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, "hw_ver", "Hardware version id", enabled_default=False)

    @property
    def native_value(self) -> int | None:
        return None if self.store.raw is None else cfgmod.hw_ver(self.store.raw)
