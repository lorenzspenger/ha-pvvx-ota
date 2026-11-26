"""Buttons: refresh the configuration, restore the settings saved before the last flash."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.components.button.const import ButtonDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import PvvxConfigEntity, wanted
from .errors import ha_error
from .shared import PvvxConfigEntry, get_shared

PARALLEL_UPDATES = 1  # one Bluetooth operation at a time, like the shared lock


async def async_setup_entry(hass: HomeAssistant, entry: PvvxConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    add(
        [
            RefreshButton(hass, entry),
            ReflashButton(hass, entry),
            RebootButton(hass, entry),
            SyncClockButton(hass, entry),
        ]
    )  # always there: it is how a failed read is tried again
    if wanted(entry.runtime_data, "adv_interval"):  # i.e. a verified firmware/layout: restore and reset make sense
        add([RestoreButton(hass, entry), ResetButton(hass, entry)])


class RefreshButton(PvvxConfigEntity, ButtonEntity):
    def __init__(self, hass: HomeAssistant, entry: PvvxConfigEntry) -> None:
        super().__init__(hass, entry, "refresh_config", enabled_default=False)

    @property
    def available(self) -> bool:
        return True  # also the way to fill a block that could not be read yet

    async def async_press(self) -> None:
        await self.store.async_refresh()


class RestoreButton(PvvxConfigEntity, ButtonEntity):
    """Writes back the settings saved just before the last flash. Never pressed automatically."""

    def __init__(self, hass: HomeAssistant, entry: PvvxConfigEntry) -> None:
        super().__init__(hass, entry, "restore_config")

    @property
    def available(self) -> bool:
        return self.store.can_restore

    async def async_press(self) -> None:
        await self.store.async_restore()


class ResetButton(PvvxConfigEntity, ButtonEntity):
    """Disabled by default. Sets the sensor's configuration block to factory defaults (also advertising type and
    encryption flag, so HA's BTHome entry may stop working). The settings before are saved and can be restored."""

    warning = (
        "Resets advertising type and the encryption flag too, so Home Assistant's BTHome link may break. "
        "The previous settings can be restored with the restore button (not the encryption flag)."
    )

    def __init__(self, hass: HomeAssistant, entry: PvvxConfigEntry) -> None:
        super().__init__(hass, entry, "reset_defaults", enabled_default=False)

    async def async_press(self) -> None:
        await self.store.async_reset_defaults()


class ReflashButton(PvvxConfigEntity, ButtonEntity):
    """Flash the latest firmware (or the beta, if that switch is on) even when the sensor is already up to date.

    Exactly the same path as Install / the flash_latest service (shared lock, settings snapshot, progress on the update
    entity, post-flash verify). Never pressed automatically."""

    warning = (
        "Takes about 1.5 to 3 minutes on a good link (longer on a weak one); settings may reset (Restore previous settings undoes that); "
        "an interrupted flash keeps the old firmware."
    )

    def __init__(self, hass: HomeAssistant, entry: PvvxConfigEntry) -> None:
        super().__init__(hass, entry, "reflash_firmware", enabled_default=False)

    @property
    def available(self) -> bool:
        shared = get_shared(self.hass)
        return (
            self.store.raw is not None  # the firmware version was read
            and self.store.flash_latest is not None
            and not shared.flashing
            and not shared.lock.locked()
        )

    async def async_press(self) -> None:
        if self.store.flash_latest is None:
            raise ha_error("update_entity_not_ready")
        await self.store.flash_latest()


class RebootButton(PvvxConfigEntity, ButtonEntity):
    """Reboot the sensor (command 0x72, "reboot on disconnect"). Harmless: it restarts in about 2 s and keeps its settings."""

    _attr_device_class = ButtonDeviceClass.RESTART

    def __init__(self, hass: HomeAssistant, entry: PvvxConfigEntry) -> None:
        super().__init__(hass, entry, "reboot_sensor", enabled_default=False)

    @property
    def available(self) -> bool:
        shared = get_shared(self.hass)
        return self.store.raw is not None and not shared.flashing and not shared.lock.locked()

    async def async_press(self) -> None:
        await self.store.async_reboot()


class SyncClockButton(PvvxConfigEntity, ButtonEntity):
    """Set the sensor's clock to the local time (command 0x23, same payload as TelinkMiFlasher's "Set Time")."""

    def __init__(self, hass: HomeAssistant, entry: PvvxConfigEntry) -> None:
        super().__init__(hass, entry, "sync_clock", enabled_default=False)

    @property
    def available(self) -> bool:
        shared = get_shared(self.hass)
        return self.store.raw is not None and not shared.flashing and not shared.lock.locked()

    async def async_press(self) -> None:
        await self.store.async_sync_clock()
