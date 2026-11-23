"""Device clock (command 0x23, no Home Assistant imports).

Verified in pvvx ``src/cmd_parser.c`` (``cmd == CMD_ID_UTC_TIME``): with a payload the first up to 4 bytes are copied into
``wrk.utc_time_sec`` (little endian uint32 seconds), the LCD is refreshed, and the reply is ``0x23`` + the 4-byte device time
(+ 4 bytes "last set time" where ``SERVICE_TIME_ADJUST`` is built in). The handler sits outside the device-specific ``#if``
blocks, so every firmware has it. TelinkMiFlasher (``setDevTime()``) sends ``0x23`` + ``Date.now()/1000 - getTimezoneOffset()*60``:
the *local wall-clock time* as epoch seconds (what the display shows), not UTC; and it offers "Set Time" for every device with
firmware >= 1.8 (the history/averaging section for >= 2.0 as well), without any per-model switch. So the integration uses
the same payload and applies it wherever the layout of the config command is verified.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime

CMD_TIME = 0x23
AUTO_TOLERANCE = 5  # seconds: the automatic sync leaves a clock that is this close alone


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
