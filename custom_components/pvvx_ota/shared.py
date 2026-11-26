"""Integration-wide state (one flash or read at a time, one firmware cache) and the entry type. No entity imports."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .firmware import FirmwareSource

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
    flashing: bool = False  # a flash is running: no configuration writes
    waiting: list[PvvxUpdateEntity] = field(
        default_factory=list
    )  # update entities queued for the firmware lock, in order
    links: dict[str, str] = field(default_factory=dict)  # address -> description of the last connection's proxy


def get_shared(hass: HomeAssistant) -> Shared:
    """Return the shared state."""
    shared: Shared = hass.data.setdefault(DOMAIN, Shared())
    if shared.source is None:
        shared.source = FirmwareSource(async_get_clientsession(hass))
    return shared
