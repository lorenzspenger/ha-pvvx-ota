"""Integration-wide state (one flash or read at a time, one firmware cache) and the entry type. No entity imports."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .firmware import STORAGE_KEY, STORAGE_VERSION, FirmwareSource

if TYPE_CHECKING:
    from .store import ConfigStore
    from .update import PvvxUpdateEntity

    type PvvxConfigEntry = ConfigEntry[ConfigStore]  # runtime_data: the sensor's ConfigStore
else:
    PvvxConfigEntry = ConfigEntry


@dataclass
class Shared:
    """Integration-wide state: one flash/read at a time, one firmware cache. Per-entry state is entry.runtime_data."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    source: FirmwareSource | None = None
    flashing_address: str | None = None  # the sensor whose image is going over right now (None: no flash runs)
    waiting: list[PvvxUpdateEntity] = field(
        default_factory=list
    )  # update entities queued for the firmware lock, in order
    silent_probes: dict[str, int] = field(
        default_factory=dict
    )  # address -> bonded-link probes in a row that got no reply (a second one pairs again)
    links: dict[str, str] = field(default_factory=dict)  # address -> description of the last connection's proxy

    @property
    def flashing(self) -> bool:
        """Some flash is running: no configuration writes for any sensor (the flash holds the connection slot)."""
        return self.flashing_address is not None

    def set_flashing(self, hass: HomeAssistant, address: str | None) -> None:
        """Record which sensor is being flashed (None: none) and notify all stores: availability is per sensor."""
        if self.flashing_address != address:
            self.flashing_address = address
            _flash_state_changed(hass)


def _flash_state_changed(hass: HomeAssistant) -> None:
    """Tell every sensor's store (and through it its entities) that the flash state moved."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        store = getattr(entry, "runtime_data", None)
        if store is not None:
            store.async_notify()


def get_shared(hass: HomeAssistant) -> Shared:
    """Return the shared state."""
    shared: Shared = hass.data.setdefault(DOMAIN, Shared())
    if shared.source is None:
        shared.source = FirmwareSource(async_get_clientsession(hass), Store(hass, STORAGE_VERSION, STORAGE_KEY))
    return shared
