"""Device-registry helpers: find the BTHome device a sensor belongs to."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import Entity


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


def attach_to_device(
    entity: Entity, hass: HomeAssistant, address: str, entry_id: str, title: str, sw_version: str | None = None
) -> str | None:
    """Put the entity on the BTHome device of this sensor if there is one, else on a device of our own; returns the id of the
    BTHome device (None: our own).

    Since Home Assistant 2026.10 a device has exactly one config entry, so this integration cannot join the BTHome device through
    ``device_info``. The entity is attached to it instead: ``device_entry`` set and no ``device_info``, as the template helper
    does. Every entity of the integration goes through here, so there is one place to change if Home Assistant changes it."""
    device = find_bthome_device(hass, address, entry_id)
    if device is not None:
        entity.device_entry = device
        return device.id
    entity._attr_device_info = DeviceInfo(
        connections={(CONNECTION_BLUETOOTH, address)}, name=title, manufacturer="pvvx", sw_version=sw_version
    )
    return None
