"""Device clock (command 0x23, no Home Assistant imports).

Verified in pvvx ``src/cmd_parser.c`` (``cmd == CMD_ID_UTC_TIME``): with a payload the first up to 4 bytes are copied into
``wrk.utc_time_sec`` (little endian uint32 seconds), the LCD is refreshed, and the reply is ``0x23`` + the 4-byte device time
(+ 4 bytes "last set time" where ``SERVICE_TIME_ADJUST`` is built in). The handler sits outside the device-specific ``#if``
blocks, so every firmware has it. TelinkMiFlasher (``setDevTime()``) sends ``0x23`` + ``Date.now()/1000 - getTimezoneOffset()*60``:
the *local wall-clock time* as epoch seconds (what the display shows), not UTC; and it offers "Set Time" for every device with
firmware >= 1.8 (the history/averaging section for >= 2.0 as well), without any per-model switch. So the integration uses
the same payload and applies it wherever the layout of the config command is verified.

Command 0x24 (``CMD_ID_TADJUST``, only where the 0x23 reply carries ``last_set``, i.e. ``SERVICE_TIME_ADJUST`` is built in) holds
the clock's tick step: the number of 16 MHz timer ticks per clock second, 16 000 000 by default, kept in the EEPROM. A read is
``0x24``, a write is ``0x24`` + signed 16 bit little endian *delta* from 16 000 000, and both are answered with ``0x24`` + the
absolute step as uint32 (docs/version-layouts.md). A larger step is a slower clock; 1 ppm is 16 ticks.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime

CMD_TIME = 0x23
AUTO_TOLERANCE = 5  # seconds: the automatic sync leaves a clock that is this close alone

CMD_TADJUST = 0x24
NOMINAL_STEP = 16_000_000  # ticks per clock second (Telink CLOCK_16M_SYS_TIMER_CLK_1S)
MIN_STEP = NOMINAL_STEP - 32768  # the write takes an int16 delta: about -2048 ppm ...
MAX_STEP = NOMINAL_STEP + 32767  # ... +2048 ppm
TICKS_PER_PPM = NOMINAL_STEP // 1_000_000
LAST_SET_MIN = 0x60000000  # a ``last_set`` below this is not a date (TelinkMiFlasher's test)
DRIFT_MIN_ELAPSED = 48 * 3600.0  # seconds between the set and the read that measures the drift (TelinkMiFlasher: 3 h)
DRIFT_MIN_OFFSET = 5.0  # seconds: a smaller offset is within the quantization and timing noise of both clocks
DRIFT_MAX_PPM = 500.0  # a crystal does not drift more: such a measurement is a restart, a manual set or a broken link
DRIFT_MIN_INTERVAL = 24 * 3600.0  # at most one adjustment attempt per sensor and day


class ClockError(Exception):
    """Unusable reply."""


def local_epoch(now: datetime) -> int:
    """Seconds since the epoch of the *local* wall-clock time (the value TelinkMiFlasher sends)."""
    offset = now.utcoffset()
    if now.tzinfo is None or offset is None:
        raise ClockError("a timezone-aware time is needed")
    return int(now.timestamp() + offset.total_seconds())


def build_set(local_seconds: int) -> bytes:
    if not 0 < local_seconds < 2**32:
        raise ClockError("time out of range")
    return bytes([CMD_TIME]) + struct.pack("<I", local_seconds)


@dataclass(frozen=True)
class DeviceTime:
    current: int
    last_set: int | None = None


def parse(reply: bytes) -> DeviceTime:
    if len(reply) < 5 or reply[0] != CMD_TIME:
        raise ClockError("unexpected reply to the clock command")
    last = struct.unpack_from("<I", reply, 5)[0] if len(reply) >= 9 else None
    return DeviceTime(struct.unpack_from("<I", reply, 1)[0], last)


def clamp_step(step: int) -> int:
    return max(MIN_STEP, min(MAX_STEP, step))


def build_step_read() -> bytes:
    return bytes([CMD_TADJUST])


def build_step_write(step: int) -> bytes:
    """``0x24`` + int16 delta from the nominal step; a step the command cannot express is refused."""
    if not MIN_STEP <= step <= MAX_STEP:
        raise ClockError("tick step out of range")
    return bytes([CMD_TADJUST]) + struct.pack("<h", step - NOMINAL_STEP)


def parse_step(reply: bytes) -> int:
    if len(reply) < 5 or reply[0] != CMD_TADJUST:
        raise ClockError("unexpected reply to the tick step command")
    return int(struct.unpack_from("<I", reply, 1)[0])


@dataclass(frozen=True)
class Drift:
    elapsed_s: float
    offset_s: float  # sensor minus host, over elapsed_s: positive means the sensor clock runs fast

    @property
    def ppm(self) -> float:
        return self.offset_s / self.elapsed_s * 1e6

    @property
    def problem(self) -> str | None:
        """Why this measurement must not be acted on (None: trustworthy enough)."""
        if abs(self.offset_s) < DRIFT_MIN_OFFSET:
            return "offset too small"
        if abs(self.ppm) > DRIFT_MAX_PPM:
            return "implausible"
        return None


def measure_drift(reading: DeviceTime, host_now: int) -> Drift | None:
    """Drift of the sensor clock since its last set, the way TelinkMiFlasher does it.

    ``last_set`` is the local time the clock was set to (by anyone, at the moment of setting), ``host_now`` the local epoch now
    (``local_epoch``). ``device_period = current - last_set`` against ``real_period = host_now - last_set``. Not comparable
    without a real date in ``last_set`` (0 after a restart, absent without TIME_ADJUST) or within DRIFT_MIN_ELAPSED of the
    set. A daylight saving change in between shifts ``real_period`` by an hour, which the 500 ppm limit rejects."""
    last = reading.last_set
    if last is None or last <= LAST_SET_MIN:
        return None
    real = host_now - last
    if real < DRIFT_MIN_ELAPSED:
        return None
    return Drift(float(real), float((reading.current - last) - real))


def corrected_step(old: int, drift: Drift) -> int:
    """The step that would have kept the clock right: the clock rate is proportional to 1/step."""
    return clamp_step(round(old * (1 + drift.offset_s / drift.elapsed_s)))
