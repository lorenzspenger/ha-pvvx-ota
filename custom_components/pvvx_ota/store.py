"""Per-sensor configuration state: cached 0x55 block, batched read-modify-write, snapshot/restore."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from bleak_retry_connector import BleakClientWithServiceCache
from homeassistant.components import bluetooth
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event, async_track_state_report_event
from homeassistant.util import dt as dt_util

from . import calib as calibmod
from . import clock as clockmod
from . import config as cfgmod
from .const import (
    CONF_AUTO_CLOCK_SYNC,
    CONF_AUTO_RESTORE_ENCRYPTION,
    CONF_CALIB_KNOWN,
    CONF_HW_ID,
    CONF_PIN,
    CONF_PIN_BONDED,
    CONF_SNAPSHOT,
    CONF_VERSION,
    DOMAIN,
    FIRST_READ_RETRY,
)
from .device import async_connected
from .errors import ha_error, validation_error
from .ota import OtaError, PairingRequiredError, parse_cfg, read_cfg_raw, send_command, write_cfg
from .registry import find_bthome_device
from .shared import get_shared

_LOGGER = logging.getLogger(__name__)
REBOOT_REPLY_TIMEOUT = 5.0
REBOOT_QUIET = 30.0  # s without other connections after the reboot command
REBOOT_WATCH = 30.0  # s to look for the first advertisement afterwards (log only)
CLOCK_TIMEOUT = 5.0
CALIB_TIMEOUT = 5.0
BATCH_WINDOW = 2.0  # seconds: changes arriving within this window share one connection


async def require_stored_key(client: BleakClientWithServiceCache) -> bytes:
    """Read-only check (command 0x18 alone) that the sensor still holds a key; a missing one answers 0xff or zeros.
    Returns the key for the one caller that carries it over a flash across the EEPROM rewrite (``ConfigStore.prepare_carry``);
    everybody else drops it. It is never logged."""
    reply = await send_command(client, b"\x18", 0x18, log=False)
    if len(reply) < 17 or set(reply[1:17]) <= {0, 0xFF}:
        raise ha_error("encryption_key_gone")
    return bytes(reply[1:17])


class ConfigStore:
    """Holds what we know of one sensor's configuration. Never polls: reads happen at setup, after a flash
    and on the refresh button."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS]
        self.name = entry.title
        self.raw: bytes | None = None
        self._listeners: list[Callable[[], None]] = []
        self._pending: dict[str, int] = {}
        self._flush: asyncio.Task[None] | None = None
        self._unreachable = False  # a read failed and none worked since: logged once, and once more when it is back
        self._writing = False  # the running flush has taken its batch and is on the wire: new changes need the next one
        self.flash_latest: Callable[[], Awaitable[None]] | None = (
            None  # set by the update entity (same path as Install)
        )
        self.last_clock: dict[str, Any] | None = None  # what the last clock sync did (diagnostics)
        self.calib: calibmod.Coef | None = None  # sensor coefficients (command 0x25), None if not readable here
        self.last_flash: dict[str, Any] | None = None  # timings/result of the last flash (diagnostics)
        self.quiet_until = 0.0  # event-loop time until which nothing else should connect (sensor is rebooting)
        self._unwatch: Callable[[], None] | None = None  # BTHome readings watched while encryption_changed is open
        self.first_read: asyncio.Task[None] | None = None  # the background first read of this setup (tests await it)
        # the bindkey read before a flash across the 5.7/5.8 EEPROM rewrite: in memory only, for this flash, never stored
        self._carry_key: bytes | None = None
        self._carry_active = False  # a flash that may cross the boundary is under way (the key may still be None)

    # -- state ---------------------------------------------------------
    @property
    def hw_id(self) -> int | None:
        return self.entry.data.get(CONF_HW_ID)

    @property
    def capabilities(self) -> set[str]:
        return cfgmod.capabilities(self.raw, self.hw_id)

    @property
    def available(self) -> bool:
        return self.raw is not None and cfgmod.is_verified(self.raw) and not get_shared(self.hass).flashing

    def value(self, key: str) -> int | None:
        """Current value, with not-yet-written changes shown optimistically."""
        if key in self._pending:
            return self._pending[key]
        return cfgmod.get(self.raw, key) if self.raw is not None and cfgmod.is_verified(self.raw) else None

    @property
    def snapshot(self) -> dict[str, Any] | None:
        return self.entry.data.get(CONF_SNAPSHOT)

    @property
    def can_restore(self) -> bool:
        """Settings differ from the snapshot, or the encryption flag that was on is off now."""
        return self._fields_differ() or self._crypto_lost()

    def _crypto_lost(self) -> bool:
        """The snapshot had encryption on and the (known, verified) block has it off: restore may switch it on again."""
        snap = self.snapshot
        raw = self.raw
        if not snap or not snap.get("crypto") or raw is None or not cfgmod.is_verified(raw):
            return False
        if self._issue_open("encryption_key_replaced"):
            return False  # the key on the sensor is not the one BTHome has: the flag alone must not be switched on
        return self.available and cfgmod.get(raw, "adv_crypto") == 0

    def _fields_differ(self) -> bool:
        """Needs only the stored snapshot. The settings are compared when the block is known; before the first read (no
        settings entity enabled) everything is written back, and the write starts from a fresh read anyway."""
        snap = self.snapshot
        if not snap or get_shared(self.hass).flashing:
            return False
        restorable = self._restorable(snap)
        if self.raw is None:
            return bool(restorable)
        return self.available and any(self.value(k) != v for k, v in restorable.items())

    def _restorable(self, snap: dict[str, Any]) -> dict[str, int]:
        caps = self.capabilities or self.stored_capabilities()
        return {k: v for k, v in snap.get("fields", {}).items() if k in caps}

    def stored_capabilities(self) -> set[str]:
        """Capabilities from the version and board stored in the entry (what entity.wanted decides from)."""
        return cfgmod.capabilities_for(self.entry.data.get(CONF_VERSION), self.hw_id)

    # -- listeners -----------------------------------------------------
    @callback
    def async_add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    @callback
    def async_notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    # -- updating from a read ------------------------------------------
    @callback
    def set_raw(self, raw: bytes) -> None:
        """Record a freshly read block (and the version/hardware id it carries)."""
        self.raw = raw
        if self._unreachable:
            self._unreachable = False
            _LOGGER.info("%s is reachable again", self.name)
        self._clear_issue("pin_bond_lost")
        if not self.entry.data.get(CONF_PIN_BONDED):
            self._clear_issue("pin_set")  # the read worked without a PIN: it was removed elsewhere
        self._clear_fixed_issues(raw)
        ver, hw = parse_cfg(raw)
        self._check_stranded_carry(raw)
        if ver is not None and (self.entry.data.get(CONF_VERSION), self.entry.data.get(CONF_HW_ID)) != (ver, hw):
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_VERSION: ver, CONF_HW_ID: hw}
            )
        self.async_notify()

    def _check_stranded_carry(self, raw: bytes) -> None:
        """A flash across the EEPROM rewrite was started with encryption on, but nobody carries the key any more (Home Assistant
        restarted before the version check): the sensor now holds a new key and has encryption off."""
        snap = self.snapshot
        if not snap or not snap.get("pending_key") or self._carry_active or get_shared(self.hass).flashing:
            return
        if not cfgmod.crosses_eep_boundary(snap.get("version"), cfgmod.version_of(raw)):
            return
        self._drop_pending()
        if cfgmod.is_verified(raw) and cfgmod.get(raw, "adv_crypto") == 0:
            _LOGGER.warning("%s: the flash replaced the encryption key and it could not be carried over", self.name)
            self._issue("encryption_key_replaced")

    def _drop_pending(self) -> None:
        snap = self.snapshot
        if snap and snap.get("pending_key"):
            rest = {k: v for k, v in snap.items() if k != "pending_key"}
            self.hass.config_entries.async_update_entry(self.entry, data={**self.entry.data, CONF_SNAPSHOT: rest})

    def _clear_fixed_issues(self, raw: bytes) -> None:
        """Issues whose cause a read shows to be gone, however it was fixed (here or with another tool)."""
        if not cfgmod.is_verified(raw):
            return
        if cfgmod.get(raw, "adv_type") == 3:
            self._clear_issue("adv_type_changed")
        if cfgmod.get(raw, "adv_crypto") == 1:
            self._clear_issue("encryption_restore_failed")
        if not self._fields_differ():
            self._clear_issue("settings_differ_after_flash")
        if self._matches_snapshot(raw) and not self._crypto_lost():  # encryption off again is still part of the reset
            self._clear_issue("reset_defaults")

    def _matches_snapshot(self, raw: bytes | None) -> bool:
        """A snapshot exists and no restorable field of this block differs from it (encryption is not part of this)."""
        snap = self.snapshot
        if not snap or raw is None or not cfgmod.is_verified(raw):
            return False
        return all(cfgmod.get(raw, k) == v for k, v in self._restorable(snap).items())

    @callback
    def take_snapshot(self) -> None:
        """Remember the settings before a flash (a flash can reset them); restoring is the user's call."""
        if self.raw is None or not cfgmod.is_verified(self.raw):
            return
        snap = {
            "version": cfgmod.version_of(self.raw),
            "fields": cfgmod.snapshot(self.raw, self.hw_id),
            # whether BTHome encryption was on (the flag only; a flash clears it, the key stays on the sensor)
            "crypto": cfgmod.get(self.raw, "adv_crypto"),
        }
        self.hass.config_entries.async_update_entry(self.entry, data={**self.entry.data, CONF_SNAPSHOT: snap})
        _LOGGER.debug("%s: settings snapshot taken: %s", self.name, snap)

    async def prepare_carry(self, client: Any, raw: bytes, target: int | None) -> None:
        """In the connection of a flash, after the snapshot: if the flash crosses the 5.7/5.8 EEPROM rewrite (``target`` None: the
        new version is unknown, so it may) and encryption is on, read the key now, because the new firmware starts with a new
        random one. It only lives in memory until the post-flash check has written it back."""
        self.clear_carry()
        old = cfgmod.version_of(raw)
        if target is not None and not cfgmod.crosses_eep_boundary(old, target):
            return
        if target is None:
            _LOGGER.info(
                "%s: the flash may cross the EEPROM rewrite of firmware 5.8, settings and key are carried over",
                self.name,
            )
        else:
            _LOGGER.info(
                "%s: this flash crosses the EEPROM rewrite of firmware 5.8, settings and encryption key are carried over",
                self.name,
            )
        if not cfgmod.is_verified(raw) or cfgmod.get(raw, "adv_crypto") != 1:
            return
        self._carry_active = True
        snap = self.snapshot
        if snap:
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_SNAPSHOT: {**snap, "pending_key": True}}
            )
        try:
            self._carry_key = await require_stored_key(client)
        except HomeAssistantError, OtaError:
            _LOGGER.warning("%s: the encryption key could not be read before the flash", self.name)

    def clear_carry(self) -> None:
        self._carry_key = None
        self._carry_active = False

    async def _read_calib(self, client: Any, raw: bytes) -> None:
        """Read the sensor coefficients in the connection we have anyway (non-fatal: older firmware lacks them)."""
        self.calib = None
        if not cfgmod.is_verified(raw) or (cfgmod.version_of(raw) or 0) < calibmod.MIN_VERSION:
            return
        try:
            self.calib = calibmod.parse(await send_command(client, b"\x25", 0x25, timeout=CALIB_TIMEOUT))
            if (
                self.entry.data.get(CONF_CALIB_KNOWN) != self.calib.known
            ):  # the offset entities depend on it (next setup)
                self.hass.config_entries.async_update_entry(
                    self.entry, data={**self.entry.data, CONF_CALIB_KNOWN: self.calib.known}
                )
        except (OtaError, calibmod.CalibError) as err:
            _LOGGER.debug("%s: no sensor coefficients: %s", self.name, err)

    @property
    def auto_clock_sync(self) -> bool:
        return bool(self.entry.options.get(CONF_AUTO_CLOCK_SYNC, True))

    async def _sync_clock(self, client: Any, force: bool = False) -> bool:
        """Set the sensor's clock to the local time (what TelinkMiFlasher sends, see clock.py); without ``force`` only when
        it is more than AUTO_TOLERANCE seconds off. Returns whether a write happened. Raises OtaError/ClockError."""
        target = clockmod.local_epoch(dt_util.now())
        before = clockmod.parse(await send_command(client, b"\x23", 0x23, timeout=CLOCK_TIMEOUT)).current
        diff = before - target
        if not force and abs(diff) <= clockmod.AUTO_TOLERANCE:
            _LOGGER.debug("%s: clock is %s s off, left alone", self.name, diff)
            return False
        after = clockmod.parse(
            await send_command(client, clockmod.build_set(target), 0x23, timeout=CLOCK_TIMEOUT)
        ).current
        if abs(after - target) > clockmod.AUTO_TOLERANCE:
            raise clockmod.ClockError(f"the sensor reports {after} after being set to {target}")
        self.last_clock = {"at": dt_util.utcnow().isoformat(), "was_off_s": diff, "forced": force}
        _LOGGER.info("%s: clock set (it was %s s off)", self.name, diff)
        return True

    async def _auto_clock(self, client: Any, raw: bytes) -> None:
        """Automatic sync inside a connection that is open anyway: never fatal, only where the layout is verified."""
        if not self.auto_clock_sync or not cfgmod.is_verified(raw):
            return
        try:
            await self._sync_clock(client)
        except (OtaError, clockmod.ClockError) as err:
            _LOGGER.debug("%s: automatic clock sync skipped: %s", self.name, err)

    async def async_sync_clock(self) -> None:
        """The "Sync clock" button: always set the time."""
        async with self._session() as client:
            raw = await read_cfg_raw(client)
            cfgmod.require_verified(raw)
            try:
                await self._sync_clock(client, force=True)
            except clockmod.ClockError as err:
                raise ha_error("sensor_error", name=self.name, error=err) from err
        self.set_raw(raw)

    def needs_read(self) -> bool:
        """Whether any settings entity of this entry is enabled. Without one nothing shows the block, so nothing reads it (the
        update entity has its own version read, "Restore previous settings" works from the stored snapshot)."""
        ent_reg = er.async_get(self.hass)
        keep = {f"{self.address}_firmware", f"{self.address}_restore_config"}
        return any(
            e.disabled_by is None and e.unique_id not in keep
            for e in er.async_entries_for_config_entry(ent_reg, self.entry.entry_id)
        )

    async def async_first_read(self, delay: float = 0.0) -> None:
        """Background read after setup: authenticated, under the shared lock like every connection. A failure leaves the
        settings entities unavailable and is retried after FIRST_READ_RETRY; after that the update entity's 12 h poll (or the
        Refresh button) fills them in."""
        if delay:
            await asyncio.sleep(delay)
        for wait in (*FIRST_READ_RETRY, None):
            if self.raw is not None or not self.needs_read():
                return
            try:
                await self.async_initial_read()
                return
            except PairingRequiredError as err:
                # a PIN will not go away by itself: no retry loop of Bluetooth connections
                self.pairing_lost(err)
                return
            except (OtaError, HomeAssistantError) as err:
                _LOGGER.debug("%s: first read failed: %s", self.name, err)
                if wait is None:
                    self.mark_unreachable(err)
                    return
                await asyncio.sleep(wait)

    def mark_unreachable(self, err: BaseException, logged: bool = False) -> None:
        """A read failed. Home Assistant's rule for unavailability: log it once (INFO), not on every failed attempt, and log
        again when the sensor is back (set_raw). ``logged``: the caller has already logged the reason."""
        if self._unreachable:
            return
        self._unreachable = True
        if not logged:
            _LOGGER.info("%s is unavailable: %s", self.name, err)

    def pairing_lost(self, err: Exception) -> None:
        """A read was refused for want of pairing. A bonded sensor was already paired again with its stored PIN by the
        connection helper if that was possible. Never an unhandled exception: a log line, a repair issue or a reauth flow,
        and the entities stay unavailable until a read works."""
        _LOGGER.warning("%s: %s", self.name, err)
        self.mark_unreachable(err, logged=True)
        if not self.entry.data.get(CONF_PIN_BONDED):
            self._issue("pin_set")
        elif isinstance(self.entry.data.get(CONF_PIN), int):
            self._issue("pin_bond_lost")  # pairing again with the stored PIN did not help
        # else: no PIN is stored; the connection helper started a reauth flow that asks for it

    async def async_initial_read(self) -> None:
        """Authenticate and read the block (setup). Raises HomeAssistantError/OtaError when that is not possible."""
        async with get_shared(self.hass).lock, async_connected(self.hass, self.address, self.name) as client:
            raw = await read_cfg_raw(client)
            await self._read_calib(client, raw)
            await self._auto_clock(client, raw)
        self.set_raw(raw)

    async def async_refresh(self) -> None:
        """Read the block now (button)."""
        shared = get_shared(self.hass)
        if shared.flashing:
            raise ha_error("flash_running")
        try:
            async with shared.lock, async_connected(self.hass, self.address, self.name) as client:
                raw = await read_cfg_raw(client)
                await self._read_calib(client, raw)
                await self._auto_clock(client, raw)
        except PairingRequiredError as err:
            self.pairing_lost(err)
            raise ha_error("sensor_error", name=self.name, error=err) from err
        except OtaError as err:
            self.mark_unreachable(err)
            raise ha_error("sensor_error", name=self.name, error=err) from err
        except HomeAssistantError as err:  # not connectable
            self.mark_unreachable(err)
            raise
        self.set_raw(raw)

    # -- writing -------------------------------------------------------
    async def async_request(self, changes: dict[str, int]) -> None:
        """Queue changes; all that arrive within BATCH_WINDOW are written over one connection.

        Returns once the batch was written and read back, raising HomeAssistantError if it failed."""
        if get_shared(self.hass).flashing:
            raise ha_error("flash_running")
        self._pending.update(changes)
        self.async_notify()
        if self._writing and self._flush is not None:
            # a batch is on the wire already and cannot take these changes: let it finish (its result belongs to its own
            # callers), then they go out in the next batch
            try:
                await asyncio.shield(self._flush)
            except Exception:  # noqa: BLE001 - reported to the callers of that batch
                pass
        if self._pending and (self._flush is None or self._flush.done()):
            self._flush = self.hass.async_create_task(self._flush_batch())
        if self._flush is not None:
            await asyncio.shield(self._flush)

    async def _flush_batch(self) -> None:
        await asyncio.sleep(BATCH_WINDOW)
        changes, self._pending = self._pending, {}
        self._writing = True
        try:
            await self._write(changes)
        finally:
            self._writing = False
            self.async_notify()  # drop the optimistic values: show what the sensor really has

    async def _write(self, changes: dict[str, int]) -> None:
        shared = get_shared(self.hass)
        async with shared.lock:
            if shared.flashing:
                raise ha_error("flash_running")
            try:
                async with async_connected(self.hass, self.address, self.name) as client:
                    # always start from a fresh read: the sensor may have been changed elsewhere meanwhile
                    raw = await read_cfg_raw(client)
                    payload = cfgmod.apply(raw, changes, parse_cfg(raw)[1])
                    if payload == raw[2:]:
                        self.set_raw(raw)
                        return
                    _LOGGER.debug("%s: writing %s -> %s", self.name, raw[2:].hex(), payload.hex())
                    echoed = await write_cfg(client, payload)
            except (OtaError, cfgmod.ConfigError) as err:
                raise ha_error("sensor_error", name=self.name, error=err) from err
        self.set_raw(echoed)
        if "adv_type" in changes and cfgmod.get(echoed, "adv_type") != 3:
            self._issue("adv_type_changed")
        stored = {k: cfgmod.get(echoed, k) for k in changes}
        if stored != changes:
            # test_config() in the firmware clamps some values
            raise ha_error("value_mismatch", name=self.name, stored=stored, requested=changes)
        if echoed[2:] != payload:
            _LOGGER.warning("%s: sensor reports %s after writing %s", self.name, echoed[2:].hex(), payload.hex())

    async def async_restore(self) -> None:
        """Write the pre-flash settings back (button; only the fields exposed here)."""
        snap = self.snapshot
        if not snap or not self.can_restore:
            raise ha_error("nothing_to_restore")
        changes = {k: v for k, v in self._restorable(snap).items() if self.value(k) != v}
        if changes:
            await self.async_request(changes)
        await self._restore_encryption(
            snap
        )  # the flag only, and only when it was on; a failure is its own repair issue
        if self._matches_snapshot(self.raw):
            self._clear_issue("reset_defaults")  # only when the read-back shows the old settings again
        self._update_settings_issue()

    # -- after a flash --------------------------------------------------
    async def async_after_flash(self) -> None:
        """Called once the post-flash version check succeeded: bring BTHome encryption back, flag differing settings."""
        snap = self.snapshot
        if not snap:
            return
        try:
            if cfgmod.crosses_eep_boundary(snap.get("version"), cfgmod.version_of(self.raw or b"")):
                await self._carry_over(snap)  # the flag alone would use the new random key: never the flag-only restore
                return
            if self.entry.options.get(CONF_AUTO_RESTORE_ENCRYPTION, True):
                await self._restore_encryption(snap)
            self._update_settings_issue()
        finally:
            self.clear_carry()
            self._drop_pending()

    async def _carry_over(self, snap: dict[str, Any]) -> None:
        """After a flash across the 5.7/5.8 EEPROM rewrite the sensor starts with defaults and a new random key: write the key
        and the settings back. Without the key (not read, or lost with a restart) encryption stays off and a repair issue says so."""
        if snap.get("crypto") and cfgmod.get(self.raw or b"", "adv_crypto") == 0:
            key, self._carry_key = self._carry_key, None
            if (
                key is not None
                and self.entry.options.get(CONF_AUTO_RESTORE_ENCRYPTION, True)
                and await self._write_back_key(key)
            ):
                self._clear_issue("encryption_key_replaced")
                _LOGGER.info("%s: BTHome encryption and its key carried over the firmware flash", self.name)
            else:
                _LOGGER.warning("%s: the flash replaced the encryption key and it was not carried over", self.name)
                self._issue("encryption_key_replaced")
        changes = {k: v for k, v in self._restorable(snap).items() if self.value(k) != v}
        if changes:
            try:
                await self.async_request(changes)
            except HomeAssistantError as err:
                _LOGGER.warning("%s: settings could not be carried over the flash: %s", self.name, err)
        if self._matches_snapshot(self.raw):
            self._clear_issue("reset_defaults")
        self._update_settings_issue()

    async def _write_back_key(self, key: bytes) -> bool:
        """Write the carried key to the sensor, check its echo, switch encryption on and check that. Never raises, never logs the key."""
        try:
            async with self._session() as client:
                raw = await read_cfg_raw(client)
                reply = await send_command(client, b"\x18" + key, 0x18, log=False)
                if bytes(reply[1:17]) != key:
                    raise ha_error("key_not_accepted", name=self.name)
                if cfgmod.get(raw, "adv_crypto"):
                    echoed = raw
                else:
                    payload = cfgmod.apply(raw, {"adv_crypto": 1}, parse_cfg(raw)[1], frozenset({"adv_crypto"}))
                    echoed = await write_cfg(client, payload)
            if cfgmod.get(echoed, "adv_crypto") != 1:
                raise ha_error("encryption_not_switched_on", name=self.name)
        except (HomeAssistantError, OtaError, cfgmod.ConfigError) as err:
            _LOGGER.warning("%s: could not write the encryption key back: %s", self.name, err)
            return False
        self.set_raw(echoed)
        self._clear_issue("encryption_restore_failed")
        return True

    def _update_settings_issue(self) -> None:
        """Other settings are never re-applied automatically: a repair issue points at the Restore button."""
        if self._fields_differ():
            self._issue("settings_differ_after_flash")
        else:
            self._clear_issue("settings_differ_after_flash")

    async def _restore_encryption(self, snap: dict[str, Any]) -> None:
        """If encryption was on in the snapshot (before a flash or a reset) and is off now, switch it on again WITHOUT writing a key: the key stays
        in the sensor's EEPROM. Verified by the block the sensor reports back. Nothing secret is logged or stored."""
        if not snap.get("crypto") or self._issue_open("encryption_key_replaced"):
            return  # it was off (or unknown), or the key on the sensor is not BTHome's: leave it alone
        raw = self.raw
        if raw is None or not cfgmod.is_verified(raw):
            return
        if cfgmod.get(raw, "adv_crypto"):
            self._clear_issue("encryption_restore_failed")
            return
        try:
            async with self._session() as client:
                raw = await read_cfg_raw(client)  # fresh block
                hw = parse_cfg(raw)[1]
                if cfgmod.get(raw, "adv_crypto"):
                    echoed = raw
                else:
                    payload = cfgmod.apply(raw, {"adv_crypto": 1}, hw, frozenset({"adv_crypto"}))
                    await require_stored_key(client)
                    echoed = await write_cfg(client, payload)
            if cfgmod.get(echoed, "adv_crypto") != 1:
                raise ha_error("encryption_not_switched_on", name=self.name)
        except (HomeAssistantError, OtaError, cfgmod.ConfigError) as err:
            _LOGGER.warning("%s: could not switch encryption back on: %s", self.name, err)
            self._issue("encryption_restore_failed")
            return
        self.set_raw(echoed)
        self._clear_issue("encryption_restore_failed")
        _LOGGER.info("%s: BTHome encryption switched back on (the key stayed on the sensor)", self.name)

    # -- dangerous operations (each behind its own guard in the caller) -----
    def _issue(self, key: str, fixable: bool = False) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{key}_{self.entry.entry_id}",
            data={"entry_id": self.entry.entry_id},
            is_fixable=fixable,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=key,
            translation_placeholders={"name": self.name},
        )

    def flash_failed(self, err: str) -> None:
        """One repair issue per sensor (id = MAC); a repeated failure updates it, the next success clears it."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"flash_failed_{self.address}",
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="flash_failed",
            translation_placeholders={"name": self.name, "error": err},
        )

    def flash_succeeded(self) -> None:
        """Also called when a version read shows the latest firmware (flashed with another tool)."""
        ir.async_delete_issue(self.hass, DOMAIN, f"flash_failed_{self.address}")
        self._clear_issue("flash_unconfirmed")

    def flash_unconfirmed_cleared(self) -> None:
        self._clear_issue("flash_unconfirmed")

    def flash_unconfirmed(self) -> None:
        """The image was sent but the new version could not be read back: it is not known whether the flash was applied."""
        self._issue("flash_unconfirmed")

    # -- encryption_changed: cleared by the first BTHome reading afterwards ------------------
    def _issue_open(self, key: str) -> bool:
        found = ir.async_get(self.hass).async_get_issue(DOMAIN, f"{key}_{self.entry.entry_id}")
        return found is not None and found.active

    def _encryption_issue_open(self) -> bool:
        return self._issue_open("encryption_changed")

    @callback
    def async_watch_bthome(self) -> None:
        """While encryption_changed is open: the first state of a BTHome entity of this sensor that is not unavailable or
        unknown clears it (that only happens once HA decrypts the sensor's adverts, i.e. the bindkey matches)."""
        self.async_unwatch_bthome()
        if not self._encryption_issue_open():
            return
        ent_reg = er.async_get(self.hass)
        device = find_bthome_device(self.hass, self.address, self.entry.entry_id)
        # only entities that carry measurements: BTHome updates the signal strength even when it cannot decrypt the advert
        ent_ids = (
            [
                e.entity_id
                for e in er.async_entries_for_device(ent_reg, device.id)
                if e.platform == "bthome"
                and e.original_device_class != SensorDeviceClass.SIGNAL_STRENGTH
                and e.entity_category != EntityCategory.DIAGNOSTIC
            ]
            if device is not None
            else []
        )

        @callback
        def seen(event: Event[Any]) -> None:
            new = event.data.get("new_state")
            if new is None or new.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                return
            if "old_state" in event.data and event.data["old_state"] is None:
                return  # the entity was just added: a state restored at start or reload, not a reading
            self._clear_issue("encryption_changed")
            self.async_unwatch_bthome()

        @callback
        def registry_changed(event: Event[Any]) -> None:
            """A BTHome entity appeared (the BTHome entry was added again): watch it too."""
            if event.data.get("action") != "create":
                return
            entry = ent_reg.async_get(event.data["entity_id"])
            device = find_bthome_device(self.hass, self.address, self.entry.entry_id)
            if entry and entry.platform == "bthome" and device is not None and entry.device_id == device.id:
                self.async_watch_bthome()

        unsubs = [self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, registry_changed)]
        if ent_ids:
            unsubs.append(async_track_state_change_event(self.hass, ent_ids, seen))
            unsubs.append(async_track_state_report_event(self.hass, ent_ids, seen))  # same value again: only reported

        def unwatch() -> None:
            for unsub in unsubs:
                unsub()

        self._unwatch = unwatch

    @callback
    def async_unwatch_bthome(self) -> None:
        if self._unwatch is not None:
            self._unwatch()
            self._unwatch = None

    def _clear_issue(self, key: str) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, f"{key}_{self.entry.entry_id}")

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[BleakClientWithServiceCache]:
        """Same rules as every write: not while flashing, one operation at a time, translated errors."""
        shared = get_shared(self.hass)
        if shared.flashing:
            raise ha_error("flash_running")
        async with shared.lock:
            if shared.flashing:
                raise ha_error("flash_running")
            try:
                async with async_connected(self.hass, self.address, self.name) as client:
                    yield client
            except (OtaError, cfgmod.ConfigError) as err:
                raise ha_error("sensor_error", name=self.name, error=err) from err

    async def async_set_encryption(
        self, enable: bool, key: str | None = None, keep_key: bool = False
    ) -> dict[str, Any]:
        """Switch encrypted adverts on/off. The key goes out only in the returned dict: never log it,
        never put it in state or attributes. The BTHome config entry is not touched.
        keep_key (with enable, no key): only the flag is set, the key already on the sensor stays; ignored when switching off."""
        keep_key = keep_key and enable
        if keep_key and key is not None:
            raise validation_error("keep_key_with_key")
        if key is not None and not re.fullmatch(r"[0-9a-fA-F]{32}", key):
            raise validation_error("bad_key")
        if keep_key and self._issue_open("encryption_key_replaced"):
            raise validation_error("keep_key_after_key_replaced")  # the key on the sensor is not the one BTHome has
        async with self._session() as client:
            raw = await read_cfg_raw(client)
            hw = parse_cfg(raw)[1]
            # computing the payload first refuses unverified firmware before anything is written
            payload = cfgmod.apply(raw, {"adv_crypto": int(enable)}, hw, frozenset({"adv_crypto"}))
            bindkey = None
            changed = cfgmod.get(raw, "adv_crypto") != int(enable)
            if keep_key:
                await require_stored_key(client)
            elif enable:
                bindkey = bytes.fromhex(key) if key else secrets.token_bytes(16)
                current = await send_command(client, b"\x18", 0x18, log=False)  # read-only: the key now on the sensor
                changed = changed or current[1:17] != bindkey
                reply = await send_command(client, b"\x18" + bindkey, 0x18, log=False)
                if reply[1:17] != bindkey:
                    raise ha_error("key_not_accepted", name=self.name)
            echoed = await write_cfg(client, payload) if payload != raw[2:] else raw
        if cfgmod.get(echoed, "adv_crypto") != int(enable):
            raise ha_error("encryption_not_switched_on" if enable else "encryption_not_switched_off", name=self.name)
        self.set_raw(echoed)
        if enable:
            self._clear_issue("encryption_restore_failed")
            self._clear_issue("encryption_key_replaced")
        if changed:  # nothing to check in BTHome when the sensor already was as requested
            self._issue("encryption_changed")
            self.async_watch_bthome()
        result: dict[str, Any] = {"encryption": enable}
        if keep_key:
            result["note"] = (
                "Encryption switched on with the key already on the sensor; the BTHome entry keeps its key."
            )
        elif bindkey is not None:
            result["bindkey"] = bindkey.hex()
            result["note"] = (
                "Enter this key in Home Assistant's BTHome entry for this sensor; it is not stored anywhere else."
            )
        else:
            result["note"] = "Encryption is off; the BTHome entry must not have a key set any more."
        return result

    async def async_set_pin(self, pin: int) -> dict[str, Any]:
        """Set (1..999999) or clear (0) the PIN. Write only: it is never read back, logged or stored."""
        if not 0 <= pin <= 999999:
            raise validation_error("bad_pin")
        async with self._session() as client:
            raw = await read_cfg_raw(client)
            cfgmod.require_verified(raw)
            reply = await send_command(client, b"\x70" + pin.to_bytes(4, "little"), 0x70, log=False)
        if len(reply) < 2 or reply[1] == 3:
            raise ha_error("pin_not_stored", name=self.name)
        if pin:
            self._issue("pin_set")
        else:
            self._clear_issue("pin_set")
        return {"pin_set": bool(pin)}

    async def async_reset_defaults(self) -> None:
        """Command 0x56: factory settings of the config block. The settings before are saved first."""
        async with self._session() as client:
            raw = await read_cfg_raw(client)
            cfgmod.require_verified(raw)
            self.set_raw(raw)
            self.take_snapshot()  # the user may bring the old settings back with the restore button
            echoed = await send_command(client, b"\x56", 0x55)
        self.set_raw(echoed)
        snap = self.snapshot
        if not self._matches_snapshot(echoed) or (snap and snap.get("crypto") and not cfgmod.get(echoed, "adv_crypto")):
            self._issue("reset_defaults", fixable=True)  # not when the sensor already was at defaults

    async def async_reboot(self) -> None:
        """Command 0x72: the sensor reboots when the link drops (pvvx "Set Reboot on disconnect"). Settings are kept.

        One short transaction under the shared lock; no reply from the sensor means it did not take the command."""
        async with self._session() as client:
            raw = await read_cfg_raw(client)  # same authentication rules as every other command
            self.set_raw(raw)
            await self._auto_clock(client, raw)  # the sensor restarts right after this connection: set its clock first
            try:
                await send_command(client, b"\x72", 0x72, timeout=REBOOT_REPLY_TIMEOUT)
            except OtaError as err:
                raise ha_error("reboot_not_confirmed", name=self.name, error=err) from err
        # leaving the session drops the link, which is what makes the sensor reboot
        self.quiet_until = asyncio.get_running_loop().time() + REBOOT_QUIET
        _LOGGER.info("%s: reboot requested, the sensor restarts when the link drops", self.name)
        self.hass.async_create_background_task(self._watch_return(), f"pvvx_ota_reboot_{self.address}")

    async def _watch_return(self) -> None:
        """Log whether the sensor advertises again within REBOOT_WATCH seconds (information only)."""
        start = time.monotonic()
        end = start + REBOOT_WATCH
        while time.monotonic() < end:
            await asyncio.sleep(2)
            info = bluetooth.async_last_service_info(self.hass, self.address, connectable=True)
            if info is not None and info.time > start:
                _LOGGER.debug(
                    "%s: advertising again %.0f s after the reboot command", self.name, time.monotonic() - start
                )
                return
        _LOGGER.debug("%s: not heard again within %.0f s after the reboot command", self.name, REBOOT_WATCH)

    async def async_set_calibration(self, temp_offset: float | None = None, humi_offset: float | None = None) -> None:
        """Change the temperature and/or humidity offset (read-modify-write of the 12 coefficient bytes: slopes and the
        other zero are written back as read), verified by the sensor's reply."""
        async with self._session() as client:
            raw = await read_cfg_raw(client)
            cfgmod.require_verified(raw)
            try:
                current = calibmod.parse(await send_command(client, b"\x25", 0x25, timeout=CALIB_TIMEOUT))
                request = calibmod.build_write(current, temp_offset, humi_offset)
                new = calibmod.parse(await send_command(client, request, 0x25, timeout=CALIB_TIMEOUT))
            except calibmod.CalibError as err:
                raise ha_error("sensor_error", name=self.name, error=err) from err
        self.calib = new
        self.async_notify()
        expected = {"tk": current.tk, "hk": current.hk}
        if temp_offset is not None and new.temp_offset != round(temp_offset, 2):
            raise ha_error("temperature_offset_mismatch", name=self.name, stored=new.temp_offset, requested=temp_offset)
        if humi_offset is not None and new.humi_offset != round(humi_offset, 2):
            raise ha_error("humidity_offset_mismatch", name=self.name, stored=new.humi_offset, requested=humi_offset)
        if (new.tk, new.hk) != (expected["tk"], expected["hk"]):
            raise ha_error("slope_changed", name=self.name)
