"""Automatic clock drift calibration (no Home Assistant imports, state in memory only).

The integration sets the sensor clock itself (``store._sync_clock``) and remembers the value and the host time. A later read that
still reports that very value as ``last_set`` (no restart, nobody else set it) tells how many seconds the sensor counted while the
host counted ``elapsed``; the difference over at least 24 h is the drift. The clock does not tick while a connection is open, so
the seconds a connection stays open after the set (and before the read) are lost on the sensor side; they bias the result towards
"slow" by at most a few seconds, which the tick step then compensates as well.

After a successful adjustment the baseline is dropped and the caller sets the clock again (a new baseline for the new rate).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import clock as clockmod
from .ota import OtaError, send_command

_LOGGER = logging.getLogger(__name__)

TIMEOUT = 5.0


@dataclass
class ClockDrift:
    set_value: int | None = None  # the clock value the integration wrote last
    set_at: datetime | None = None  # host time of that write
    last_try: datetime | None = None  # the last attempt to change the step (at most one per day)
    last: dict[str, Any] | None = None  # the last measurement and what was done with it (diagnostics, not stable)

    def note_set(self, value: int, at: datetime) -> None:
        self.set_value, self.set_at = value, at

    def forget(self) -> None:
        self.set_value = self.set_at = None

    async def check(self, client: Any, reading: clockmod.DeviceTime, now: datetime, name: str) -> bool:
        """Measure at the start of a connection and adjust the tick step when it is warranted. Returns True when the step
        changed (or may have changed): the baseline is gone and the caller must set the clock again. Never raises."""
        if self.set_value is None or self.set_at is None or reading.last_set is None:
            return False
        drift = clockmod.measure_drift(reading, self.set_value, (now - self.set_at).total_seconds())
        if drift is None:
            return False
        info: dict[str, Any] = {
            "at": now.isoformat(),
            "elapsed_s": round(drift.elapsed_s),
            "offset_s": round(drift.offset_s, 1),
            "ppm": round(drift.ppm, 1),
            "old_step": None,
            "new_step": None,
            "result": "",
        }
        self.last = info
        info["result"] = drift.problem or ""
        if drift.problem:
            return False
        if self.last_try is not None and (now - self.last_try).total_seconds() < clockmod.DRIFT_MIN_INTERVAL:
            info["result"] = "rate limited"
            return False
        self.last_try = now
        try:
            return await self._adjust(client, drift, info, name)
        except (OtaError, clockmod.ClockError) as err:
            info["result"] = f"failed: {err}"
            _LOGGER.debug("%s: clock drift adjustment skipped: %s", name, err)
            return False

    async def _adjust(self, client: Any, drift: clockmod.Drift, info: dict[str, Any], name: str) -> bool:
        old = clockmod.parse_step(await send_command(client, clockmod.build_step_read(), clockmod.CMD_TADJUST, TIMEOUT))
        info["old_step"] = old
        if not clockmod.MIN_STEP <= old <= clockmod.MAX_STEP:
            info["result"] = "unexpected step"  # not a nominal 16 MHz timer: the arithmetic does not apply
            return False
        new = clockmod.corrected_step(old, drift)
        if new == old:
            info["result"] = "no change"  # at the end of the range, or too small to be one tick
            return False
        info["new_step"] = new
        await send_command(client, clockmod.build_step_write(new), clockmod.CMD_TADJUST, TIMEOUT)
        stored = clockmod.parse_step(
            await send_command(client, clockmod.build_step_read(), clockmod.CMD_TADJUST, TIMEOUT)
        )
        if stored != new:
            info["result"] = "read-back mismatch"
            _LOGGER.warning("%s: the sensor reports tick step %s after writing %s; not retried", name, stored, new)
            if stored == old:
                return False
            self.forget()
            return True
        info["result"] = "adjusted"
        _LOGGER.info(
            "%s: clock drift %.1f ppm over %.1f h, tick step %s -> %s",
            name,
            drift.ppm,
            drift.elapsed_s / 3600,
            old,
            new,
        )
        self.forget()
        return True
