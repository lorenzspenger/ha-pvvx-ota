"""Device-registry helpers: find the BTHome device a sensor belongs to."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr


def find_devices(hass: HomeAssistant, address: str, exclude_entry_id: str | None = None) -> list[dr.DeviceEntry]:
    """Devices with this Bluetooth connection (not owned by exclude_entry_id), BTHome's first."""
    found = [
        d
        for d in dr.async_get(hass).async_get_devices(connections={(dr.CONNECTION_BLUETOOTH, address)})
        if d.config_entry_id != exclude_entry_id
    ]

    def _is_bthome(device: dr.DeviceEntry) -> bool:
        entry = hass.config_entries.async_get_entry(device.config_entry_id)
        return entry is not None and entry.domain == "bthome"

    return sorted(found, key=lambda d: not _is_bthome(d))


def find_bthome_device(hass: HomeAssistant, address: str, own_entry_id: str) -> dr.DeviceEntry | None:
    """The existing device (normally BTHome's) this integration's entity should sit on."""
    devices = find_devices(hass, address, own_entry_id)
    return devices[0] if devices else None
