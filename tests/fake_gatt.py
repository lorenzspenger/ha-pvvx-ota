"""Fake GATT client emulating the pvvx OTA characteristic."""

import asyncio
import secrets
import struct
import time

from bleak.exc import BleakError

from custom_components.pvvx_ota.ota import CFG_CHAR, OTA_CHAR


class FakeClient:
    @property
    def device_time(self) -> int:
        return self._clock_base + int(self.time_source() - self._clock_set_at)

    @device_time.setter
    def device_time(self, value: int) -> None:
        self._clock_base = value
        self._clock_set_at = self.time_source()

    def reboot(self, stale: int = 1_000_000) -> None:
        """Sensor restart: the clock falls back to a stale value (last history record) and last_set to 0."""
        self.device_time = stale
        if self.last_set is not None:
            self.last_set = 0

    def __init__(
        self,
        cfg: bytes | None = None,
        fail_status_at: int | None = None,
        status_code: int = 2,
        hang_write_at: int | None = None,
        no_reply: bool = False,
        end_marker_raises: bool = False,
        after_marker: str = "reboot",
        needs_pairing: bool = False,
        pair_fails: bool = False,
        pair_helps: bool = True,
        link_error_at: int | None = None,
    ):
        self.link_error_at = link_error_at  # OTA write number at which the link fails (BleakError)
        self.needs_pairing = needs_pairing  # config char refuses until paired
        self.pair_fails = pair_fails
        self.pair_helps = pair_helps
        self.pair_calls = 0
        self.cfg_writes: list[bytes] = []
        self.bindkey = bytes(16)
        self.pin: int | None = None
        self.pin_writes = 0
        self.pin_fails = False
        self.resets = 0
        # SHTC3 factory coefficients: Tk 17500, Hk 10000, Tz -45.00, Hz 0, id, i2c 0x70, chip type 1
        self.coef = struct.pack("<IIhhIBB", 17500, 10000, -4500, 0, 0x12345678, 0x70, 1)
        self.coef_writes: list[bytes] = []
        self.time_source = time.time  # controllable: the sensor clock ticks with it
        self._clock_base = 0
        self._clock_set_at = self.time_source()
        self.time_reads = 0
        self.drop_set = False  # the sensor echoes a set but does not keep it (restores a stale clock)
        self.last_set: int | None = None  # None: 4-byte reply (no TIME_ADJUST); an int adds the 4-byte last-set time
        self.time_writes: list[bytes] = []
        self.no_clock_reply = False
        self.key_writes = 0
        self.reboots = 0
        self.no_reboot_reply = False
        self.default_cfg: bytes | None = None
        self.flash_to: int | None = None  # version byte the sensor reports after the end marker (None: unchanged)
        self.eep_defaults: bytes | None = None  # config bytes after the version byte once a flash erased the EEPROM
        self.key_echo_wrong = False  # a 0x18 write is answered with another key
        self.key_reads = 0
        self.clamp = lambda b: b  # emulate the firmware's test_config() clamping
        self.cfg = cfg if cfg is not None else bytes([0x55, 0x59, 0, 0, 0, 0, 10, 4, 0x8B, 0, 55, 0])
        self.fail_status_at = fail_status_at
        self.status_code = status_code
        self.hang_write_at = hang_write_at
        self.no_reply = no_reply
        self.end_marker_raises = end_marker_raises
        self.after_marker = after_marker  # reboot | stay | crc_error | bleak | stall | stall_link_down
        self.marker_sent = False
        self.writes: list[bytes] = []
        self.responses: list[bool] = []
        self.status_reads = 0
        self.disconnected = False
        self.connected_flag = True  # what is_connected reports
        self.link_drops_after: float | None = (
            None  # after_marker="silent": seconds after the marker until the link counts as lost
        )
        self.marker_at = 0.0
        self._cb = None

    @property
    def is_connected(self) -> bool:
        return self.connected_flag

    async def pair(self):
        self.pair_calls += 1
        if self.pair_fails:
            raise RuntimeError("Pairing failed due to error: no passkey")
        if self.pair_helps:
            self.needs_pairing = False
        return True

    async def start_notify(self, char, cb):
        assert char == CFG_CHAR
        if self.needs_pairing:
            raise PermissionError("Bluetooth GATT Error handle=45 error=15 description=Insufficient encryption")
        self._cb = cb

    async def stop_notify(self, char):
        self._cb = None

    async def write_gatt_char(self, char, data, response):
        if char == CFG_CHAR:
            if self.needs_pairing:
                raise PermissionError("Bluetooth GATT Error handle=45 error=15 description=Insufficient encryption")
            if data[:1] == b"\x18":  # bindkey get/set (16 bytes)
                if len(data) == 17:
                    self.bindkey = bytes(data[1:])
                    self.key_writes += 1
                    self._cb(char, bytearray(b"\x18" + (bytes(16) if self.key_echo_wrong else self.bindkey)))
                elif self.bindkey in (bytes(16), b"\xff" * 16):
                    self._cb(char, bytearray([0x18, 0xFF]))  # no key in the EEPROM
                else:
                    self.key_reads += 1
                    self._cb(char, bytearray(b"\x18" + self.bindkey))
                return
            if data[:1] == b"\x70":  # set PIN (u32 LE); reply 1 = stored, 3 = failed
                self.pin_writes += 1
                self.pin = int.from_bytes(data[1:5], "little")
                self._cb(char, bytearray([0x70, 3 if self.pin_fails else 1]))
                return
            if data[:1] == b"\x23":  # device clock: get (no payload) / set (4 bytes LE); reply 0x23 + time
                if len(data) >= 5:
                    self.time_writes.append(bytes(data))
                    self.device_time = int.from_bytes(data[1:5], "little")
                    if self.last_set is not None:
                        self.last_set = self.device_time
                else:
                    self.time_reads += 1
                if not self.no_clock_reply:
                    reply = b"\x23" + self.device_time.to_bytes(4, "little")
                    if self.last_set is not None:
                        reply += self.last_set.to_bytes(4, "little")
                    self._cb(char, bytearray(reply))
                if len(data) >= 5 and self.drop_set:
                    self.reboot()
                return
            if data[:1] == b"\x25":  # sensor coefficients: get (no payload) / set (12 bytes), reply 0x25 + 18 bytes
                if len(data) > 1:
                    self.coef_writes.append(bytes(data))
                    self.coef = bytes(data[1:13]) + self.coef[12:]
                self._cb(char, bytearray(b"\x25" + self.coef))
                return
            if data[:1] == b"\x72":  # reboot on disconnect; reply 72 xx
                self.reboots += 1
                if not self.no_reboot_reply:
                    self._cb(char, bytearray([0x72, 0x00]))
                return
            if data[:1] == b"\x56":  # reset to defaults
                self.resets += 1
                self.cfg = self.default_cfg or self.cfg
                self._cb(char, bytearray(self.cfg))
                return
            if data[:1] == b"\x55" and len(data) > 1:  # write config: 0x55 + cfg bytes (no version byte)
                self.cfg_writes.append(bytes(data))
                new = bytearray(self.cfg[:2]) + bytearray(data[1:])
                new = bytearray(self.clamp(bytes(new)))
                self.cfg = bytes(new)
            if not self.no_reply:
                self._cb(char, bytearray(self.cfg))
            return
        assert char == OTA_CHAR
        if self.hang_write_at is not None and len(self.writes) == self.hang_write_at:
            await asyncio.sleep(3600)
        if self.link_error_at is not None and len(self.writes) == self.link_error_at:
            raise BleakError("Device disconnected")
        if data[:2] == b"\x02\xff":
            self.marker_sent = True
            self.marker_at = time.monotonic()
            self._apply_flash()
        if data[:2] == b"\x02\xff" and self.end_marker_raises:
            raise ConnectionError("link dropped (device rebooting)")
        if data[:2] == b"\x02\xff":
            self.marker_sent = True
        self.writes.append(bytes(data))
        self.responses.append(response)

    def _apply_flash(self) -> None:
        """The new image starts: the encryption flag is off after any flash; across the 5.7/5.8 EEPROM rewrite the whole
        config goes back to ``eep_defaults`` and the sensor makes up a new random bindkey."""
        if self.flash_to is None:
            return
        old = self.cfg[1]
        if (old < 0x58) != (self.flash_to < 0x58):
            body = self.eep_defaults if self.eep_defaults is not None else self.cfg[2:]
            self.bindkey = secrets.token_bytes(16)
        else:
            body = self.cfg[2:]
        body = bytearray(body)
        body[1] &= ~0x08 & 0xFF  # flg2 bit 3: adv_crypto
        self.cfg = bytes([0x55, self.flash_to]) + bytes(body)

    async def read_gatt_char(self, char):
        if self.marker_sent:
            if self.after_marker == "reboot":
                raise ConnectionError("disconnected")
            if self.after_marker == "bleak":
                raise BleakError("Device disconnected")
            if (
                self.after_marker == "silent"
            ):  # a rebooted sensor: no answers, the link counts as lost only after the supervision timeout
                if self.link_drops_after is not None and time.monotonic() - self.marker_at >= self.link_drops_after:
                    self.connected_flag = False
                raise TimeoutError()
            if self.after_marker == "stall":  # no answer, the link stays up
                raise TimeoutError()
            if self.after_marker == "stall_link_down":  # no answer, and the client says the link is gone
                self.connected_flag = False
                raise TimeoutError()
            return bytearray([6 if self.after_marker == "crc_error" else 0])
        self.status_reads += 1
        # blocks written so far = len(writes) - 2 start commands
        if self.fail_status_at is not None and len(self.writes) - 2 > self.fail_status_at:
            return bytearray([self.status_code])
        return bytearray([0])

    async def disconnect(self):
        self.disconnected = True
