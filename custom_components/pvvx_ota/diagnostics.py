"""Config entry diagnostics. The bindkey is never stored by this integration. The PIN of a PIN-bonded sensor is stored in the config entry on purpose (to pair again when the proxy loses the bond), so the entry data is redacted here. Bluetooth addresses and proxy names are pseudonymised (see `_Pseudonyms`) because the dump is posted in public issues."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from . import config as cfgmod
from .const import DOMAIN
from .shared import PvvxConfigEntry, get_shared

TO_REDACT = {"bindkey", "bind_key", "key", "pin", "pincode", "password", "token", "passkey"}

_MAC = r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}"
_MAC_RE = re.compile(_MAC)
# describe_link(): "<proxy name> (<MAC>), RSSI -60" (or "<name>, RSSI x" when the name is the MAC itself)
_LINK_RE = re.compile(rf"^(?P<name>.*?)(?: \((?P<mac>{_MAC})\))?, RSSI (?P<rssi>.*)$", re.DOTALL)
_NAMED_RE = re.compile(rf"[^,;()\n]+? \((?P<mac>{_MAC})\)")  # "<proxy name> (<MAC>)" anywhere in a string
_LINK_KEYS = {"last_connection_via", "link"}


class _Pseudonyms:
    """Bluetooth addresses and proxy names are replaced for the dump (it gets posted in public issues). A MAC keeps
    only its last three bytes ("XX:XX:XX:0A:CF:E5"), enough to tell sensors and proxies apart and to match them with
    the log; a proxy name (it can hold a room) becomes "proxy_N" per proxy, numbered in order of appearance."""

    def __init__(self) -> None:
        self._proxies: dict[str, str] = {}

    @staticmethod
    def mac(text: str) -> str:
        return _MAC_RE.sub(lambda m: "XX:XX:XX:" + m.group(0)[9:].upper().replace("-", ":"), text)

    def named(self, text: str) -> str:
        """Also drops the name in front of "(<MAC>)": ESPHome proxies are called "<node> (<MAC>)"."""
        return self.mac(_NAMED_RE.sub(lambda m: f"{self._proxy(m['mac'])} ({m['mac']})", text))

    def _proxy(self, key: str) -> str:
        return self._proxies.setdefault(key.upper(), f"proxy_{len(self._proxies) + 1}")

    def link(self, text: str) -> str:
        if not (m := _LINK_RE.match(text)):
            return self.named(text)
        name, address = m["name"], m["mac"]
        if address is None and _MAC_RE.fullmatch(name):
            address, name = name, ""
        label = self._proxy(address or name)
        suffix = f" ({self.mac(address)})" if address else ""
        return f"{label}{suffix}, RSSI {m['rssi']}"

    def walk(self, value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            return {k: self.walk(v, k if isinstance(k, str) else None) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.walk(v, key) for v in value]
        if isinstance(value, str):
            return self.link(value) if key in _LINK_KEYS else self.named(value)
        return value


_TITLE_SUFFIX = re.compile(r"\((?:[0-9A-Fa-f]{2}:)*[0-9A-Fa-f]{2}\)\s*$")


def _title(title: str) -> str:
    """User-chosen titles can hold a room name: keep only the "(EA:CB:69)" suffix of the default title."""
    return f"sensor {m.group(0)}" if (m := _TITLE_SUFFIX.search(title)) else "sensor"


def _entity_ids(entities: list[er.RegistryEntry], address: str) -> list[str]:
    """Entity ids hold area and user names: use "<domain>.<translation key>" (or the unique id after the address)."""
    out = []
    for n, e in enumerate(sorted(entities, key=lambda e: e.unique_id), 1):
        key = e.translation_key or e.unique_id.removeprefix(address).lstrip("_")
        out.append(f"{e.domain}.{key}" if key and key != e.unique_id else f"{e.domain}.entity_{n}")
    return sorted(out)


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


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: PvvxConfigEntry) -> dict[str, Any]:
    """Diagnostics for one sensor."""
    shared = get_shared(hass)
    store = getattr(entry, "runtime_data", None)
    address: str = entry.data.get(CONF_ADDRESS, "")

    ent_reg, dev_reg = er.async_get(hass), dr.async_get(hass)
    entities = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    device_ids = {e.device_id for e in entities if e.device_id}
    devices: list[dict[str, Any]] = []
    for device_id in sorted(device_ids):
        device = dev_reg.async_get(device_id)
        if device is None:
            continue
        owner = hass.config_entries.async_get_entry(device.config_entry_id)
        devices.append(
            {
                "name": f"device_{len(devices) + 1}",
                "model": getattr(device, "model", None),
                "owned_by": owner.domain if owner else None,  # normally bthome: the entities sit on its device
                "own_device": device.config_entry_id == entry.entry_id,
                "connections": sorted(list(c) for c in getattr(device, "connections", ())),
            }
        )

    data = {
        "entry": {"title": _title(entry.title), "data": dict(entry.data), "options": dict(entry.options)},
        "config_block": _decode(None if store is None else store.raw, None if store is None else store.hw_id),
        "capabilities": sorted(store.capabilities) if store is not None else [],
        "firmware_catalog": shared.source.summary() if shared.source is not None else None,
        "last_clock_read": None if store is None else store.last_clock_read,
        "clock_drift": None if store is None else store.drift.last,
        "last_flash": None if store is None else store.last_flash,
        "last_connection_via": shared.links.get(address),
        "flashing_now": shared.flashing,
        "snapshot": None if store is None else store.snapshot,
        "devices": devices,
        "entities": _entity_ids(entities, address),
        "domain": DOMAIN,
    }
    redacted: dict[str, Any] = _Pseudonyms().walk(async_redact_data(data, TO_REDACT))
    return redacted
