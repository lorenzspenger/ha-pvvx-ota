"""Config entry diagnostics. The bindkey is never stored by this integration. The PIN of a PIN-bonded sensor is stored in the config entry on purpose (to pair again when the proxy loses the bond), so the entry data is redacted here."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from . import config as cfgmod
from . import get_shared
from .const import DOMAIN

TO_REDACT = {"bindkey", "bind_key", "key", "pin", "pincode", "password", "token", "passkey"}


def _decode(raw: bytes | None, hw_id: int | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    out: dict[str, Any] = {
        "raw_hex": raw.hex(),  # the 0x55 config block; it holds no key (the bindkey has its own command)
        "firmware_version": None if (v := cfgmod.version_of(raw)) is None else f"{v >> 4}.{v & 15}",
        "layout_verified": cfgmod.is_verified(raw),
        "hw_ver_byte": cfgmod.hw_ver(raw),
    }
    if cfgmod.is_verified(raw):
        out["fields"] = {k: cfgmod.get(raw, k) for k in sorted(cfgmod.capabilities(raw, hw_id) | {"adv_crypto"})}
    return out


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Diagnostics for one sensor."""
    shared = get_shared(hass)
    store = getattr(entry, "runtime_data", None)
    address: str = entry.data.get(CONF_ADDRESS, "")

    ent_reg, dev_reg = er.async_get(hass), dr.async_get(hass)
    entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    device_ids = {e.device_id for e in entities if e.device_id}
    devices = []
    for device_id in device_ids:
        device = dev_reg.async_get(device_id)
        if device is None:
            continue
        owner = hass.config_entries.async_get_entry(device.config_entry_id)
        devices.append(
            {
                "name": device.name_by_user or device.name,
                "model": getattr(device, "model", None),
                "owned_by": owner.domain if owner else None,  # normally bthome: the entities sit on its device
                "own_device": device.config_entry_id == entry.entry_id,
                "connections": sorted(list(c) for c in getattr(device, "connections", ())),
            }
        )

    data = {
        "entry": {"title": entry.title, "data": dict(entry.data), "options": dict(entry.options)},
        "config_block": _decode(None if store is None else store.raw, None if store is None else store.hw_id),
        "capabilities": sorted(store.capabilities) if store is not None else [],
        "firmware_catalog": shared.source.summary() if shared.source is not None else None,
        "last_flash": None if store is None else store.last_flash,
        "last_connection_via": shared.links.get(address),
        "flashing_now": shared.flashing,
        "snapshot": None if store is None else store.snapshot,
        "devices": devices,
        "entities": sorted(e.entity_id for e in entities),
        "domain": DOMAIN,
    }
    return async_redact_data(data, TO_REDACT)
