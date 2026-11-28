"""State of one firmware flash of one sensor, from the snapshot to the end of the post-flash version check.

One ``FlashSession`` per flash, owned by the sensor's ``ConfigStore`` (``store.session``); the update entity drives it. It holds
what used to be spread over the entity and the store: the expected version, the unverified flag, the key carried across the
5.7/5.8 EEPROM rewrite (memory only), the quiet period, the version-check timer and task, the persisted ``verify_pending``
marker, and the facts the unload logic needs (transfer task, end marker sent, detached). No entity imports.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .calib import Coef
from .const import CONF_VERIFY_PENDING, POST_FLASH_DELAY

if TYPE_CHECKING:
    from .store import ConfigStore


class FlashSession:
    """One flash of one sensor. The carried key is never logged, stored or part of the repr."""

    def __init__(self, store: ConfigStore, expected: int | None = None, unverified: bool = False) -> None:
        self._store = store
        self.expected = expected  # the version byte the flash should leave on the sensor (None: a local file)
        self.unverified = unverified  # the flash ended without a clear result
        # the bindkey read before a flash across the 5.7/5.8 EEPROM rewrite: in memory only, for this flash, never stored
        self._carry_key: bytes | None = None
        # the sensor coefficients read before such a flash (memory only; the flash resets them to the factory values)
        self._carry_calib: Coef | None = None
        self.carry_active = False  # a flash that may cross the boundary is under way (the key may still be None)
        self.quiet_until = 0.0  # event-loop time until which nothing else should connect (the sensor is rebooting)
        self.transfer_task: asyncio.Task[Any] | None = None  # the task that runs the flash (an unload cancels it)
        self.transferring = False  # the flash itself (connection, transfer, end marker) is running
        self.end_marker_sent = False  # from here on the sensor may apply the image: the transfer must not be cut off
        self.stop_requested = False  # the entry is being unloaded and the transfer was cancelled for that
        self.detached = False  # the entity is gone (unload, removal): nothing is scheduled for it any more
        self._cancel_timer: Callable[[], None] | None = None
        self.verify_task: asyncio.Task[None] | None = None

    def __repr__(self) -> str:
        return f"<FlashSession expected={self.expected} unverified={self.unverified} end_marker_sent={self.end_marker_sent}>"

    def mark_end_marker_sent(self) -> None:
        self.end_marker_sent = True

    # -- the carried key -----------------------------------------------
    @property
    def carry_key(self) -> bytes | None:
        return self._carry_key

    def set_carry_key(self, key: bytes) -> None:
        self._carry_key = key

    def take_carry_key(self) -> bytes | None:
        key, self._carry_key = self._carry_key, None
        return key

    @property
    def carry_calib(self) -> Coef | None:
        return self._carry_calib

    def set_carry_calib(self, coef: Coef) -> None:
        self._carry_calib = coef

    def take_carry_calib(self) -> Coef | None:
        coef, self._carry_calib = self._carry_calib, None
        return coef

    def clear_carry(self) -> None:
        self._carry_key = None
        self._carry_calib = None
        self.carry_active = False

    # -- the persisted marker (entry data, no secrets) -------------------
    def write_marker(self) -> None:
        """Persist that a post-flash check is outstanding. Never for an entry that is gone."""
        if not self._store.entry_alive:
            return
        marker = {"expected": self.expected, "unverified": self.unverified, "at": dt_util.utcnow().timestamp()}
        entry = self._store.entry
        self._store.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_VERIFY_PENDING: marker})

    def clear_marker(self) -> None:
        entry = self._store.entry
        if self._store.entry_alive and CONF_VERIFY_PENDING in entry.data:
            data = {k: v for k, v in entry.data.items() if k != CONF_VERIFY_PENDING}
            self._store.hass.config_entries.async_update_entry(entry, data=data)

    # -- quiet period and the version check ----------------------------------
    def start_quiet(self, seconds: float) -> None:
        self.quiet_until = asyncio.get_running_loop().time() + seconds

    def schedule_verify(self, run: Callable[[], Coroutine[Any, Any, None]], delay: float | None = None) -> None:
        """Run ``run()`` as a background task of the entry after ``delay`` (default POST_FLASH_DELAY). A newer call replaces a
        timer that is still waiting. Nothing is scheduled once the session is detached: the marker resumes the check."""
        self.stop_timer()
        if self.detached:
            return

        @callback
        def _start(now: Any) -> None:
            # a plain lambda here ran in an executor thread ("loop is not the running loop") and never started the check
            self._cancel_timer = None
            if not self.detached:
                self.verify_task = self._store.entry.async_create_background_task(
                    self._store.hass, run(), "pvvx_ota_verify"
                )

        self._cancel_timer = async_call_later(self._store.hass, POST_FLASH_DELAY if delay is None else delay, _start)

    def stop_timer(self) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None

    def detach(self) -> None:
        """The entity goes away: a waiting check stops (the marker in the entry data resumes it at the next setup, the
        carried key does not survive). Cancelling a running transfer is the caller's job (it knows the phase)."""
        self.detached = True
        self.stop_timer()
