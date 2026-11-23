"""Temperature/humidity calibration offsets: the sensor-coefficient command (no Home Assistant imports).

Since firmware 4.7 the offsets are not in the 0x55 config block any more. Command ``CMD_ID_CFS = 0x25``
(pvvx ``src/cmd_parser.h``) gets/sets the sensor coefficients; verified in ``src/cmd_parser.c`` (``cmd == CMD_ID_CFS``:
``memcpy(&sensor_cfg.coef, &req->dat[1], len)`` with len limited to ``sizeof(sensor_cfg.coef)`` = 12, flash write, then the
reply is ``0x25`` + the first ``sensor_cfg_send_size`` = 18 bytes of ``sensor_cfg``) and in ``src/sensor.h``
(``sensor_coef_t``: ``u32 val1_k, u32 val2_k, s16 val1_z, s16 val2_z``; then ``u32 id, u8 i2c_addr, u8 sensor_type``).
The struct and the handler are identical in the sources of v5.7 and master (5.9); the handler and ``sensor_cfg_send_size`` = 18 are the same since 4.8
(4.7 replies with 17 bytes and has no chip type, 4.6 and older have no such command), and the factory ``def_thcoef_*`` tables below
are identical at every tag from 4.9 to 5.5 (4.8 lacks only the CHT8215 table). TelinkMiFlasher labels the same fields
"Slope factor"/"Zero offset" (``T = RegT*Tk/65536 + Tz``, ``H = RegH*Hk/65536 + Hz``, in 0.01 units, little endian).

Tz/Hz include the sensor chip's own constant (e.g. SHTC3: -45.00 degC), so an *offset* is the difference to the factory
value of that chip type, taken from the firmware's ``def_thcoef_*`` tables in ``src/sensors.c`` (v5.7 and master agree).
For a chip type not in the table the offsets are not offered (nothing is guessed). Slopes and everything else are kept.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

CMD_CFS = 0x25
REPLY_LEN = 19  # 0x25 + 18 bytes
MIN_VERSION = 0x48  # 4.7 replies with 17 bytes and no chip type, earlier versions do not know the command
COEF_LEN = 12

# ID_SENSOR_TYPES -> (factory Tz, factory Hz) in 0.01 units, from src/sensors.c def_thcoef_*
FACTORY_Z = {
    1: (-4500, 0),  # SHTC3
    2: (-4500, -600),  # SHT4x
    3: (-4500, 0),  # SHT30
    4: (-4000, 0),  # CHT8305
    5: (-5000, 0),  # AHT2x
    6: (0, 0),  # CHT8215
}
MAX_OFFSET = 12.5


class CalibError(Exception):
    """The coefficient block cannot be used safely."""


@dataclass(frozen=True)
class Coef:
    tk: int
    hk: int
    tz: int
    hz: int
    sensor_id: int
    sensor_type: int
    raw: bytes  # the 12 coefficient bytes as read

    @property
    def known(self) -> bool:
        return self.sensor_type in FACTORY_Z

    @property
    def temp_offset(self) -> float | None:
        return round((self.tz - FACTORY_Z[self.sensor_type][0]) / 100, 2) if self.known else None

    @property
    def humi_offset(self) -> float | None:
        return round((self.hz - FACTORY_Z[self.sensor_type][1]) / 100, 2) if self.known else None


def parse(reply: bytes) -> Coef:
    """Decode a reply to command 0x25."""
    if len(reply) < REPLY_LEN or reply[0] != CMD_CFS:
        raise CalibError("unexpected reply to the sensor-coefficient command")
    tk, hk, tz, hz, sensor_id = struct.unpack_from("<IIhhI", reply, 1)
    return Coef(tk, hk, tz, hz, sensor_id, reply[18], bytes(reply[1:13]))


def build_write(current: Coef, temp_offset: float | None = None, humi_offset: float | None = None) -> bytes:
    """Request for command 0x25 that changes only Tz and/or Hz; Tk, Hk and the other bytes are written back unchanged."""
    if not current.known:
        raise CalibError(f"sensor chip type {current.sensor_type} is not known, offsets are not offered")
    tz, hz = current.tz, current.hz
    for name, value in (("temperature", temp_offset), ("humidity", humi_offset)):
        if value is not None and not -MAX_OFFSET <= value <= MAX_OFFSET:
            raise CalibError(f"{name} offset {value} is outside -{MAX_OFFSET}..{MAX_OFFSET}")
    if temp_offset is not None:
        tz = FACTORY_Z[current.sensor_type][0] + round(temp_offset * 100)
    if humi_offset is not None:
        hz = FACTORY_Z[current.sensor_type][1] + round(humi_offset * 100)
    return bytes([CMD_CFS]) + struct.pack("<IIhh", current.tk, current.hk, tz, hz)
