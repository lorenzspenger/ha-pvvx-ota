"""pvvx ATC_MiThermometer firmware updates over Home Assistant's Bluetooth stack."""

from __future__ import annotations

import logging

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType

from .const import CONF_BONDED_SCANNER, CONF_PIN_BONDED, DOMAIN, FIRST_READ_STAGGER
from .errors import not_ready
from .pairing import async_forget_bond
from .shared import PvvxConfigEntry, Shared, get_shared
from .store import ConfigStore
from .update import async_register_services

__all__ = ["DOMAIN", "PvvxConfigEntry", "Shared", "get_shared"]

_LOGGER = logging.getLogger(__name__)

ISSUE_KEYS = (
    "adv_type_changed",
    "encryption_changed",
    "encryption_restore_failed",
    "flash_unconfirmed",
    "pin_bond_lost",
    "pin_set",
    "reset_defaults",
    "settings_differ_after_flash",
)
PLATFORMS = [Platform.UPDATE, Platform.NUMBER, Platform.SELECT, Platform.SWITCH, Platform.BUTTON, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the entity service."""
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PvvxConfigEntry) -> bool:
    """Set up a sensor. No Bluetooth connection is made here: the entities are decided from the stored board and version, and
    the first read of the configuration block runs in the background (ConfigStore.async_first_read)."""
    address: str = entry.data[CONF_ADDRESS]
    # cheap test-before-setup: some connectable scanner has to have heard the sensor lately
    if async_ble_device_from_address(hass, address, connectable=True) is None:
        raise not_ready("sensor_not_heard", name=entry.title, address=address)
    store = ConfigStore(hass, entry)
    entry.runtime_data = store
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _remove_empty_own_devices(hass, entry)
    _schedule_first_read(hass, entry, store)
    return True


def _schedule_first_read(hass: HomeAssistant, entry: PvvxConfigEntry, store: ConfigStore) -> None:
    """One background read per entry: right away when HA is already running (reload, new entry), else after HA has started,
    the entries spaced FIRST_READ_STAGGER apart so that they do not all hit the proxies at once."""
    if hass.state is CoreState.running:
        delay = 0.0
    else:
        entries = hass.config_entries.async_entries(DOMAIN)
        delay = FIRST_READ_STAGGER * (entries.index(entry) if entry in entries else 0)

    @callback
    def start(_hass: HomeAssistant) -> None:
        store.first_read = entry.async_create_background_task(
            hass, store.async_first_read(delay), "pvvx_ota first read"
        )

    if (unsub := async_at_started(hass, start)) is not None:
        entry.async_on_unload(unsub)


def _remove_empty_own_devices(hass: HomeAssistant, entry: PvvxConfigEntry) -> None:
    """Drop devices this entry created earlier that no longer hold an entity (entity moved to BTHome's)."""
    dev_reg, ent_reg = dr.async_get(hass), er.async_get(hass)
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        if not er.async_entries_for_device(ent_reg, device.id, include_disabled_entities=True):
            dev_reg.async_remove_device(device.id)


async def async_remove_entry(hass: HomeAssistant, entry: PvvxConfigEntry) -> None:
    """The entry is gone for good: so are its repair issues."""
    for key in ISSUE_KEYS:
        ir.async_delete_issue(hass, DOMAIN, f"{key}_{entry.entry_id}")
    if entry.data.get(CONF_ADDRESS):
        ir.async_delete_issue(hass, DOMAIN, f"flash_failed_{entry.data[CONF_ADDRESS]}")
    if entry.data.get(CONF_PIN_BONDED) and entry.data.get(CONF_BONDED_SCANNER) and entry.data.get(CONF_ADDRESS):
        await async_forget_bond(hass, entry.data[CONF_ADDRESS], entry.data[CONF_BONDED_SCANNER])


async def async_unload_entry(hass: HomeAssistant, entry: PvvxConfigEntry) -> bool:
    """Unload."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
