"""Update entity: reads the installed version over GATT and flashes pvvx firmware."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.service import async_register_platform_entity_service
from homeassistant.util import dt as dt_util

from .const import (
    CATALOG_POLL_INTERVAL,
    CATALOG_RETRY,
    CONF_BETA,
    CONF_HW_ID,
    CONF_MIN_BATTERY_V,
    CONF_POLL_INTERVAL,
    CONF_VERSION,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MIN_BATTERY_V,
    POLL_CHOICES,
    POST_FLASH_ATTEMPTS,
    POST_FLASH_DELAY,
    POST_FLASH_QUIET,
    POST_FLASH_RETRY,
    QUEUE_TIMEOUT,
    WEAK_RSSI,
)
from .device import async_connected, describe_link, expected_path
from .errors import ha_error, validation_error
from .firmware import (
    HISTORY_PAGE,
    Catalog,
    Target,
    check_local_image,
    choose_target,
    notes_between,
    plain,
)
from .ota import (
    FirmwareError,
    OtaError,
    OtaUnverified,
    PairingRequiredError,
    flash,
    fmt_version,
    parse_cfg,
    parse_version,
    read_cfg_raw,
)
from .registry import attach_to_device, find_bthome_device
from .shared import PvvxConfigEntry, get_shared

PARALLEL_UPDATES = 0  # not limited here: the firmware queue (shared lock) already runs one operation at a time, and
# it needs the concurrent calls to queue them ("Update all"); a semaphore would hide the queue position
_LOGGER = logging.getLogger(__name__)
SERVICE_FLASH_FILE = "flash_file"
SERVICE_FLASH_LATEST = "flash_latest"
SERVICE_SET_ENCRYPTION = "set_encryption"
SERVICE_SET_PIN = "set_pin"


# Home Assistant's own schema types come from probatio now while cv/vol still hand out voluptuous objects: typed loosely here
_SCHEMAS: dict[str, dict[Any, Any]] = {
    "flash_file": {
        vol.Required("path"): cv.string,
        vol.Optional("force", default=False): cv.boolean,
        vol.Optional("skip_board_check", default=False): cv.boolean,
    },
    "flash_latest": {
        vol.Optional("status_every", default=8): vol.All(vol.Coerce(int), vol.Range(min=1, max=64)),
        vol.Optional("force", default=False): cv.boolean,
    },
    "set_encryption": {vol.Required("enable"): cv.boolean, vol.Optional("key"): cv.string},
    "set_pin": {
        vol.Required("pin"): vol.All(vol.Coerce(int), vol.Range(min=0, max=999999)),
        vol.Optional("confirm", default=False): cv.boolean,
    },
}


def async_register_services(hass: HomeAssistant) -> None:
    """Register entity services (called once from async_setup)."""
    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_FLASH_FILE,
        entity_domain="update",
        schema=_SCHEMAS["flash_file"],
        func="async_flash_file",
        admin_only=True,
    )
    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_FLASH_LATEST,
        entity_domain="update",
        schema=_SCHEMAS["flash_latest"],
        func="async_flash_latest",
        admin_only=True,
    )

    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_ENCRYPTION,
        entity_domain="update",
        schema=_SCHEMAS["set_encryption"],
        func="async_set_encryption",
        admin_only=True,
        supports_response=SupportsResponse.OPTIONAL,
    )
    async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_PIN,
        entity_domain="update",
        schema=_SCHEMAS["set_pin"],
        func="async_set_pin",
        admin_only=True,
        supports_response=SupportsResponse.OPTIONAL,
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: PvvxConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the update entity."""
    async_add_entities([PvvxUpdateEntity(hass, entry)])


class PvvxUpdateEntity(UpdateEntity):
    """Firmware update of one sensor."""

    _attr_has_entity_name = True
    _attr_name = None
    # volatile or diagnostic: a new state row on every change would fill the database (the signal strength moves with every
    # advertisement, the queue position with every operation). in_progress/update_percentage are already excluded by the
    # update component itself.
    _unrecorded_attributes = frozenset({"expected_proxy", "expected_proxy_rssi", "queued_position", "last_error"})
    _attr_should_poll = False  # own timers: the catalog (HTTP) and the device read (Bluetooth, an option) run apart
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS | UpdateEntityFeature.RELEASE_NOTES
    )

    def __init__(self, hass: HomeAssistant, entry: PvvxConfigEntry) -> None:
        self._entry = entry
        self._address: str = entry.data[CONF_ADDRESS]
        self._name = entry.title
        self._attr_unique_id = f"{self._address}_firmware"
        # on the BTHome device if there is one, else on our own; _registry_event re-links later
        self._linked_id = attach_to_device(self, hass, self._address, entry.entry_id, entry.title)
        self._store_cfg = entry.runtime_data
        self._hw_id: int | None = entry.data.get(CONF_HW_ID)
        self._version: int | None = entry.data.get(CONF_VERSION)
        self._catalog: Catalog | None = None
        self._catalog_error: str | None = None
        self._queued_task: asyncio.Task[None] | None = None  # set while this entity waits in the firmware queue
        self._quiet_until = 0.0  # event-loop time until which async_update opens no connection (after a flash)
        self._cancel_device_poll: Callable[[], None] | None = None
        self._cancel_verify: Callable[[], None] | None = None  # the timer of the post-flash version check
        self._poll_seconds_set: int | None = -1  # the interval the timer runs with (-1: none scheduled yet)

    async def async_added_to_hass(self) -> None:
        """Re-link when the BTHome device appears or disappears."""
        await super().async_added_to_hass()
        self.async_on_remove(self.hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._registry_event))
        self.async_on_remove(self._store_cfg.async_add_listener(self._cfg_changed))
        self.async_on_remove(self._entry.add_update_listener(self._entry_updated))
        self._store_cfg.flash_latest = self.async_flash_latest  # the "Reflash firmware" button reuses this path
        self._store_cfg.async_notify()  # the button may have been added before this entity: refresh its availability
        self.async_on_remove(lambda: setattr(self._store_cfg, "flash_latest", None))
        self._entry.async_create_task(self.hass, self._async_first_update(), "pvvx_ota first read")
        self.async_on_remove(
            async_track_time_interval(self.hass, self._catalog_poll_due, timedelta(seconds=CATALOG_POLL_INTERVAL))
        )
        self.async_on_remove(self._stop_device_poll)
        self.async_on_remove(self._stop_verify)
        self._schedule_device_poll()

    async def _async_first_update(self) -> None:
        """The setup already read the sensor (no entity exists without that), so only the catalog is needed. If it cannot be
        fetched the entity shows *unknown* and the fetch is retried soon (HTTP only, no Bluetooth), not only at the 12 h poll."""
        shared = get_shared(self.hass)
        assert shared.source is not None
        missing = False
        try:
            self._catalog = await shared.source.async_catalog()
        except FirmwareError as err:
            _LOGGER.warning("%s", err)
            missing = True
        await shared.source.async_history()
        if missing or not shared.source.history:
            self._schedule_catalog_retry(0)  # catalog or release notes missing: try again soon (HTTP only)
        self._cfg_changed()
        self.async_write_ha_state()

    @callback
    def _schedule_catalog_retry(self, attempt: int) -> None:
        if attempt >= len(CATALOG_RETRY):
            return  # from here on the 12 h poll tries again
        cancel = async_call_later(self.hass, CATALOG_RETRY[attempt], lambda _now: self._catalog_retry_due(attempt))
        self.async_on_remove(cancel)

    @callback
    def _catalog_retry_due(self, attempt: int) -> None:
        self._entry.async_create_task(self.hass, self._async_catalog_retry(attempt), "pvvx_ota catalog retry")

    async def _async_catalog_retry(self, attempt: int) -> None:
        shared = get_shared(self.hass)
        assert shared.source is not None
        if self._catalog is not None and shared.source.history:
            return
        try:
            self._catalog = await shared.source.async_catalog()
        except FirmwareError as err:
            _LOGGER.debug("%s (retry %d)", err, attempt + 1)
            self._schedule_catalog_retry(attempt + 1)
            return
        await shared.source.async_history()
        self.async_write_ha_state()
        if not shared.source.history:
            self._schedule_catalog_retry(attempt + 1)

    async def _entry_updated(self, hass: HomeAssistant, entry: PvvxConfigEntry) -> None:
        """The entry changed (beta switch, poll interval, a version stored by a read): re-evaluate what is offered, and
        reschedule the device read only if its interval option changed (an unrelated update must not restart the timer)."""
        if self._poll_seconds() != self._poll_seconds_set:
            self._schedule_device_poll()
        self.async_write_ha_state()

    # -- timers ----------------------------------------------------------
    def _poll_seconds(self) -> int | None:
        """The configured interval of the Bluetooth version read in seconds, None for "never" (on demand, after a flash)."""
        choice = self._entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        return POLL_CHOICES[choice] if choice in POLL_CHOICES else POLL_CHOICES[DEFAULT_POLL_INTERVAL]

    @callback
    def _stop_verify(self) -> None:
        if self._cancel_verify is not None:
            self._cancel_verify()
            self._cancel_verify = None

    @callback
    def _stop_device_poll(self) -> None:
        if self._cancel_device_poll is not None:
            self._cancel_device_poll()
            self._cancel_device_poll = None

    @callback
    def _schedule_device_poll(self) -> None:
        """(Re)start the timer of the device read from the option; applies without a restart."""
        self._stop_device_poll()
        seconds = self._poll_seconds()
        self._poll_seconds_set = seconds
        if seconds is not None:
            self._cancel_device_poll = async_track_time_interval(
                self.hass, self._device_poll_due, timedelta(seconds=seconds)
            )

    @callback
    def _device_poll_due(self, _now: Any) -> None:
        self._entry.async_create_task(self.hass, self._async_poll_device_and_write(), "pvvx_ota device poll")

    async def _async_poll_device_and_write(self) -> None:
        await self._async_poll_device()
        self.async_write_ha_state()

    @callback
    def _catalog_poll_due(self, _now: Any) -> None:
        self._entry.async_create_task(self.hass, self._async_refresh_catalog_and_write(), "pvvx_ota catalog refresh")

    async def _async_refresh_catalog_and_write(self) -> None:
        await self._async_refresh_catalog()
        self.async_write_ha_state()

    @callback
    def _cfg_changed(self) -> None:
        """A read through the config store (e.g. the refresh button) may carry a new version."""
        if self._store_cfg.raw is None:
            return
        ver, hw = parse_cfg(self._store_cfg.raw)
        if ver is not None and (ver, hw) != (self._version, self._hw_id):
            self._version, self._hw_id = ver, hw
            self.async_write_ha_state()

    @callback
    def _registry_event(self, event: Any) -> None:
        if event.data["action"] == "update" and "connections" not in event.data.get("changes", {}):
            return
        device = find_bthome_device(self.hass, self._address, self._entry.entry_id)
        if (device.id if device else None) != self._linked_id:
            _LOGGER.debug("Device registry changed for %s, reloading to re-link", self._address)
            self.hass.config_entries.async_schedule_reload(self._entry.entry_id)

    # -- state ---------------------------------------------------------
    @property
    def installed_version(self) -> str | None:
        return fmt_version(self._version)

    @property
    def beta_wanted(self) -> bool:
        return bool(self._entry.options.get(CONF_BETA, False))

    def _target(self) -> Target | None:
        return None if self._catalog is None else choose_target(self._catalog, self._hw_id, self.beta_wanted)

    @property
    def latest_version(self) -> str | None:
        target = self._target()
        if target is None:
            return None  # unknown (no catalog yet): neither "up to date" nor an update
        # firmware.json stores the version byte in decimal (89 == 0x59 == V5.9)
        return fmt_version(target.version)

    @property
    def release_url(self) -> str:
        return HISTORY_PAGE  # pvvx's "Firmware version history" table

    @property
    def _history(self) -> dict[int, str]:
        """pvvx's README version history from the one shared cache (empty until a fetch succeeded)."""
        source = get_shared(self.hass).source
        return {} if source is None else source.history

    def _is_current(self, target: Target) -> bool:
        return self._version is not None and self._version >= target.version

    @property
    def release_summary(self) -> str | None:
        target = self._target()
        if target is None:
            return None
        if target.is_beta:
            return f"Beta firmware V{fmt_version(target.version)} (pre-release, not for everyday use)"
        if target.note:
            return f"Beta firmware: {target.note}; staying on stable"
        # up to date: what the installed version brought; otherwise what the offered one brings
        row = self._history.get(
            self._version if self._is_current(target) and self._version is not None else target.version
        )
        return plain(row) if row else None

    async def async_release_notes(self) -> str | None:
        """What changed between the installed and the offered version (pvvx's README history), newest first. An up to date
        sensor gets the notes of the version it runs."""
        target = self._target()
        if target is None:
            return None
        if self._is_current(target):
            row = None if self._version is None else self._history.get(self._version)
            if not row:
                return None
            return f"**V{fmt_version(self._version)}** (installed, current): {row}\n\nSource: {HISTORY_PAGE}"
        notes = notes_between(self._history, self._version, target.version)
        if notes is None:
            return (
                "No release notes were found in pvvx's README for these versions. "
                f"See {HISTORY_PAGE} and https://github.com/pvvx/ATC_MiThermometer/commits/master"
            )
        return notes + f"\n\nSource: {HISTORY_PAGE}"

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        latest, installed = parse_version(latest_version), parse_version(installed_version)
        return latest is not None and installed is not None and latest > installed

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {"hardware_id": self._hw_id}
        if self._catalog is not None:
            attrs["beta_version"] = fmt_version(self._catalog.beta)
            attrs["beta_firmware_option"] = self.beta_wanted
            target = self._target()
            if target is not None and target.note:
                attrs["beta_status"] = target.note
            try:
                attrs["board"] = self._catalog.board_for(self._hw_id)
            except FirmwareError:
                attrs["board"] = None
        if self._catalog_error:
            attrs["last_error"] = self._catalog_error
        path = expected_path(self.hass, self._address)
        if path is not None:
            attrs["expected_proxy"], attrs["expected_proxy_rssi"] = (
                path  # the likely path of the next connection or flash
            )
        waiting = get_shared(self.hass).waiting
        if self in waiting:
            attrs["queued_position"] = waiting.index(self) + 1
        return attrs

    # -- polling -------------------------------------------------------
    async def async_update(self) -> None:
        """On demand (homeassistant.update_entity): refresh the firmware list and read the installed version. The two also run on
        their own schedules (the list every 12 h over HTTP, the read per the poll-interval option)."""
        await self._async_refresh_catalog()
        await self._async_poll_device()

    async def _async_refresh_catalog(self) -> None:
        """The firmware list and release notes (HTTP only). Update offers depend on this, never on the Bluetooth poll interval."""
        shared = get_shared(self.hass)
        assert shared.source is not None
        try:
            self._catalog = await shared.source.async_catalog()
        except FirmwareError as err:
            _LOGGER.warning("%s", err)
        await shared.source.async_history()

    async def _async_poll_device(self) -> None:
        """Read the installed version over Bluetooth, best effort."""
        if asyncio.get_running_loop().time() < max(self._quiet_until, self._store_cfg.quiet_until):
            # HA's own refresh right after the flash service would connect to a rebooting sensor (and a proxy that still
            # holds the old link refuses it); the scheduled post-flash check reads it later
            _LOGGER.debug("%s: version read skipped, the sensor is rebooting after a flash", self._name)
            return
        try:
            await self._async_read_device()
            self._catalog_error = None
        except HomeAssistantError as err:
            # not connectable now: keep the last known version, retry next poll
            self._catalog_error = str(err)
            self._store_cfg.mark_unreachable(err)  # one INFO line until it works again
            _LOGGER.debug("%s: version read skipped: %s", self._name, err)

    async def _async_read_device(self) -> None:
        shared = get_shared(self.hass)
        try:
            async with shared.lock, async_connected(self.hass, self._address, self._name) as client:
                raw = await read_cfg_raw(client)
        except PairingRequiredError as err:
            self._store_cfg.pairing_lost(err)
            raise ha_error("sensor_error", name=self._name, error=err) from err
        except OtaError as err:
            raise ha_error("sensor_error", name=self._name, error=err) from err
        ver, hw = parse_cfg(raw)
        self._store(ver, hw)
        self._store_cfg.set_raw(raw)

    def _store(self, ver: int | None, hw: int | None) -> None:
        if ver is None:
            raise ha_error("unrecognised_reply", name=self._name)
        self._version, self._hw_id = ver, hw
        self.hass.config_entries.async_update_entry(
            self._entry, data={**self._entry.data, CONF_VERSION: ver, CONF_HW_ID: hw}
        )

    # -- install -------------------------------------------------------
    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Install the latest release image for this device's board."""
        await self._async_flash(None, None)

    async def async_flash_latest(self, status_every: int = 8, force: bool = False) -> None:
        """Entity service: flash the latest release (even if up to date) with a tunable status interval."""
        await self._async_flash(None, None, status_every, force)

    async def async_flash_file(self, path: str, force: bool = False, skip_board_check: bool = False) -> None:
        """Entity service: flash a local image (the file name must name the device's board, unless ``skip_board_check``)."""
        source = get_shared(self.hass).source
        assert source is not None
        if not skip_board_check:
            try:
                await source.async_catalog()  # live or the stored copy: the board is derived from it
            except FirmwareError as err:
                raise validation_error("catalog_needed_for_file", error=err) from err
        if not self.hass.config.is_allowed_path(path):
            raise validation_error("path_not_allowed", path=path)
        try:
            data = await self.hass.async_add_executor_job(Path(path).read_bytes)
        except OSError as err:
            raise validation_error("file_unreadable", path=path, error=err) from err
        await self._async_flash(data, Path(path).name, force=force, skip_board_check=skip_board_check)

    def _battery_voltage(self) -> float | None:
        """Battery voltage from the linked BTHome device's voltage sensor (no extra Bluetooth traffic), None if unknown."""
        device_id = self.registry_entry.device_id if self.registry_entry else None
        if device_id is None:
            return None
        for ent in er.async_entries_for_device(er.async_get(self.hass), device_id):
            state = self.hass.states.get(ent.entity_id)
            if state is None or ent.platform == DOMAIN:
                continue
            if (ent.device_class or ent.original_device_class) == "voltage" or state.attributes.get(
                "device_class"
            ) == "voltage":
                try:
                    volts = float(state.state)
                except ValueError:
                    continue
                if state.attributes.get("unit_of_measurement") == "mV":
                    volts /= 1000
                return volts
        return None

    def _check_battery(self, force: bool) -> None:
        """Refuse to flash a nearly empty sensor (a flash that dies half way is not what you want)."""
        volts = self._battery_voltage()
        limit = float(self._entry.options.get(CONF_MIN_BATTERY_V, MIN_BATTERY_V))
        if volts is None:
            _LOGGER.warning("%s: battery voltage unknown (no BTHome voltage sensor); flashing anyway", self._name)
        elif volts < limit:
            if not force:
                raise validation_error("battery_low", name=self._name, volts=f"{volts:.2f}", limit=f"{limit:.2f}")
            _LOGGER.warning("%s: battery %.2f V is below %.2f V, flashing anyway (force)", self._name, volts, limit)
        else:
            _LOGGER.debug("%s: battery %.2f V", self._name, volts)

    @asynccontextmanager
    async def _firmware_slot(self, shared: Any) -> AsyncIterator[None]:
        """One firmware/config operation at a time, first come first served: a second one waits (shown as in_progress
        with a queued_position) instead of being refused, so "Update all" flashes the sensors one after another."""
        if shared.lock.locked():
            shared.waiting.append(self)
            self._queued_task = asyncio.current_task()
            self._attr_in_progress = True  # queued: in progress without a percentage
            self._attr_update_percentage = None
            for waiting in list(shared.waiting):
                waiting.async_write_ha_state()
            _LOGGER.debug("%s: waiting for the firmware lock, position %s", self._name, len(shared.waiting))
            acquired = False
            try:
                async with asyncio.timeout(QUEUE_TIMEOUT):
                    await shared.lock.acquire()
                acquired = True
            except TimeoutError as err:
                raise ha_error("queue_timeout", name=self._name, hours=f"{QUEUE_TIMEOUT / 3600:.0f}") from err
            finally:
                shared.waiting.remove(self)
                self._queued_task = None
                if not acquired:  # timeout or cancelled (HA stopping, entry unloaded): leave cleanly
                    self._attr_in_progress = False
                for waiting in [self, *shared.waiting]:
                    if waiting.hass is not None and waiting.entity_id:
                        waiting.async_write_ha_state()
        else:
            await shared.lock.acquire()
        try:
            yield
        finally:
            shared.lock.release()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel a queued operation cleanly when the entry is unloaded or removed."""
        task = self._queued_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _weak_signal_notice(self) -> str | None:
        """A warning (never a refusal) if the proxy the flash will go through hears the sensor weakly."""
        path = expected_path(self.hass, self._address)
        if path is None or path[1] >= WEAK_RSSI:
            return None
        return (
            f"weak signal: {path[1]} dBm via {path[0]}, a flash through this link may be slow or fail; "
            "the transfer is stopped early if it is too slow"
        )

    async def _async_flash(
        self,
        local: bytes | None,
        filename: str | None,
        status_every: int = 8,
        force: bool = False,
        skip_board_check: bool = False,
    ) -> None:
        shared = get_shared(self.hass)
        assert shared.source is not None
        async with self._firmware_slot(shared):
            record: dict[str, Any] = {"started": dt_util.utcnow().isoformat(), "result": "failed before the transfer"}
            expected: int | None = None  # the version byte this flash should leave on the sensor (None: a local file)
            notice = self._weak_signal_notice()
            if notice:
                _LOGGER.warning("%s", notice)
            try:
                self._check_battery(force)
                # a forced local file may go without any catalog: then there is no board to check it against
                catalog: Catalog | None = None
                if not (local is not None and skip_board_check):
                    catalog = await shared.source.async_catalog()
                async with async_connected(self.hass, self._address, self._name) as client:
                    t_conn = asyncio.get_running_loop().time()  # for the "time since connect" timing logs
                    raw = await read_cfg_raw(client)
                    ver, hw = parse_cfg(raw)
                    if ver is None:
                        raise FirmwareError("device did not answer like pvvx custom firmware; not flashing")
                    self._store(ver, hw)
                    self._store_cfg.set_raw(raw)
                    await self._store_cfg._auto_clock(client, raw)  # in this connection anyway; never fatal
                    self._store_cfg.take_snapshot()  # a flash can reset settings; the user may restore them
                    if local is None:
                        assert catalog is not None
                        target = choose_target(catalog, hw, self.beta_wanted)
                        # the beta image already passed the board check in choose_target; size/CRC follow in download
                        fw = await shared.source.async_download(target.source or catalog.image_for(hw))
                    else:
                        assert filename is not None
                        if skip_board_check:
                            _LOGGER.warning(
                                "%s: flashing %s WITHOUT checking that it is the right board for this sensor (skip_board_check)",
                                self._name,
                                filename,
                            )
                            check_local_image(local, filename, None)
                        else:
                            assert catalog is not None
                            check_local_image(local, filename, catalog.board_for(hw))  # raises for unknown hardware ids
                        fw = local
                    self._attr_update_percentage = 0
                    self._attr_in_progress = True
                    shared.flashing = True  # no configuration writes while the image is going over
                    self._store_cfg.async_notify()
                    self.async_write_ha_state()
                    record["link"] = describe_link(self.hass, client, self._address)
                    record["beta"] = bool(local is None and target.is_beta)
                    expected = target.version if local is None else None
                    _LOGGER.info(
                        "%s: flashing %s via %s",
                        self._name,
                        f"V{fmt_version(target.version)}" if local is None else filename,
                        record["link"],
                    )
                    stats = await flash(
                        client,
                        fw,
                        self._set_progress,
                        status_every=status_every,
                        connected_at=t_conn,
                        context=f"connected via {record['link']}",
                    )
                    record.update(
                        stats, result="ok", blocks_per_s=round(stats["blocks"] / max(stats["seconds"], 0.1), 1)
                    )
                    _LOGGER.info("%s: flash finished in %.0f s", self._name, stats["seconds"])
            except OtaError as err:
                text = f"{err} ({notice})" if notice else str(err)
                unverified = isinstance(err, OtaUnverified)
                record["result"] = f"unverified: {text}" if unverified else f"failed: {text}"
                self._store_cfg.flash_failed(
                    text
                )  # stays until a later flash, or the version check below, shows the new version
                if unverified:
                    # the image may have been applied: read the version back like after a normal flash
                    self._quiet_until = asyncio.get_running_loop().time() + POST_FLASH_QUIET
                    self._schedule_verify(expected, unverified=True)
                raise ha_error("flash_failed", name=self._name, error=text) from err
            finally:
                self._store_cfg.last_flash = record
                shared.flashing = False
                self._store_cfg.async_notify()
                self._attr_in_progress = False
                self._attr_update_percentage = None
                self.async_write_ha_state()
        self._store_cfg.flash_succeeded()
        self._quiet_until = asyncio.get_running_loop().time() + POST_FLASH_QUIET
        self._schedule_verify()

    async def async_set_encryption(self, enable: bool, key: str | None = None) -> dict[str, Any]:
        """Entity service. The bindkey is returned in the response data only (never state, attributes or log)."""
        return await self._store_cfg.async_set_encryption(enable, key)

    async def async_set_pin(self, pin: int, confirm: bool = False) -> dict[str, Any]:
        """Entity service. With a PIN set, HA's Bluetooth stack/proxies (and so this integration) cannot connect any more."""
        if confirm is not True:
            raise validation_error("pin_needs_confirmation")
        return await self._store_cfg.async_set_pin(pin)

    @callback
    def _set_progress(self, pct: int) -> None:
        self._attr_update_percentage = pct
        self.async_write_ha_state()

    # -- post-flash verification ---------------------------------------
    async def _async_verify_after_flash(self, expected: int | None = None, unverified: bool = False) -> None:
        """Read the new version (up to POST_FLASH_ATTEMPTS tries, POST_FLASH_RETRY s apart), then bring encryption back and flag differing settings.

        ``unverified``: the flash ended without a clear result (the link stalled after the end marker). Reading the expected
        version then clears the failure issue; if the version cannot be read at all a repair issue says the flash may not
        have been applied."""
        for attempt in range(POST_FLASH_ATTEMPTS):
            try:
                await self._async_read_device()
                self.async_write_ha_state()
                self._store_cfg.flash_unconfirmed_cleared()
                if unverified and expected is not None and self._version is not None and self._version >= expected:
                    self._store_cfg.flash_succeeded()
                await self._store_cfg.async_after_flash()  # encryption flag back on, differing settings flagged
                return
            except HomeAssistantError as err:
                if isinstance(err.__cause__, PairingRequiredError):
                    return  # logged, reported and (if possible) re-paired by the read: retrying would not help
                _LOGGER.debug(
                    "%s: post-flash version check %s/%s failed: %s", self._name, attempt + 1, POST_FLASH_ATTEMPTS, err
                )
                await asyncio.sleep(POST_FLASH_RETRY)
        _LOGGER.warning(
            "%s: could not confirm the new version after flashing; installed version left unchanged",
            self._name,
        )
        self._store_cfg.flash_unconfirmed()

    def _schedule_verify(self, expected: int | None = None, unverified: bool = False) -> None:
        @callback
        def _start(now: Any) -> None:
            # a plain lambda here ran in an executor thread ("loop is not the running loop") and never started the check
            self._entry.async_create_background_task(
                self.hass, self._async_verify_after_flash(expected, unverified), "pvvx_ota_verify"
            )

        if self._cancel_verify is not None:
            self._cancel_verify()  # a newer flash replaces the check that is still waiting
        self._cancel_verify = async_call_later(self.hass, POST_FLASH_DELAY, _start)
