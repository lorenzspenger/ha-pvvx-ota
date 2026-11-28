"""Automatic clock drift calibration (no Home Assistant imports, no state that must survive a restart).

The same method as pvvx's TelinkMiFlasher: the 0x23 reply carries ``last_set``, the local time the clock was set to at the moment
of setting (by this integration or any other tool), so ``current - last_set`` is what the sensor counted and ``host now -
last_set`` what really passed. The ratio corrects the tick step. Nothing is remembered between connections, so it works across
Home Assistant restarts; it cannot work after a sensor restart (``last_set`` is 0). The reading is taken at the start of the
connection, because the sensor clock stands still while connected. After a change the caller sets the clock again, which is the
new baseline.
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
    last_try: datetime | None = None  # the last attempt to change the step (at most one per day)
    last: dict[str, Any] | None = None  # the last measurement and what was done with it (diagnostics, not stable)

    async def check(self, client: Any, reading: clockmod.DeviceTime, host_now: int, now: datetime, name: str) -> bool:
        """Measure at the start of a connection and adjust the tick step when it is warranted. Returns True when the step
        changed (or may have changed): the baseline is gone and the caller must set the clock again. Never raises."""
        drift = clockmod.measure_drift(reading, host_now)
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
            return stored != old  # the rate may have changed: the caller sets the clock again
        info["result"] = "adjusted"
        _LOGGER.info(
            "%s: clock drift %.1f ppm over %.1f h, tick step %s -> %s",
            name,
            drift.ppm,
            drift.elapsed_s / 3600,
            old,
            new,
        )
        return True
