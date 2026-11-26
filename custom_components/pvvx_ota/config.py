"""pvvx device configuration block (command 0x55): layout, fields, read-modify-write.

No Home Assistant imports. A reply to command 0x55 is ``0x55, <firmware version>, cfg[0..N]`` where cfg is
the firmware's ``cfg_t`` (src/app.h). Writing sends ``0x55`` + the cfg bytes (everything after the version byte);
the firmware does ``memcpy(&cfg, &req->dat[1], len)`` and then ``test_config()``, which clamps some values.

Layout verified against pvvx/ATC_MiThermometer ``src/app.h`` at every tag from v4.2 to v5.5 and the commits of 5.7, 5.8 and
master (5.9) (evidence and the differences per version: docs/version-layouts.md). From 4.2 on the bytes used here have the same
position; fields a version lacks are listed in VERSION_EXCLUDED. Before 4.7 bytes 4-5 are the temperature/humidity offsets (not
exposed, kept as read), 4.7-5.0 reserved bytes. Since 4.7 the bytes after ``flg2`` are ``flg3`` and
``event_adv_cnt`` (the temperature/humidity offsets moved to the separate sensor-coefficient command), so
python-interface's ``atc_mi_construct.cfg`` (offsets at that place) is stale and must NOT be used for these versions.
Any other firmware version is refused until its layout is compared.

Display bits are board specific: some bits of ``flg``/``flg2``/``flg3`` mean different things per ``DEVICE_TYPE`` (the
``#if`` blocks in ``cfg_t``). Each key in FIELDS has one fixed position, and the per-board sets in DISPLAY_BY_HW say which
keys a hardware id may use (no two keys of one board share bits, a test checks this). Boards not listed there get no
display fields at all. Evidence per board: the notes in docs/display-layouts.md.

Dangerous settings (exposed only behind explicit guardrails, see docs/usage.md): advertising type (leaving BTHome makes
HA's ``bthome`` stop seeing the sensor), long range / BT5 PHY, encryption (``adv_crypto`` + bindkey, via an action that
sets the key first) and the PIN (command 0x70, write only). Not exposed at all: ``adv_flags`` (flg2 bit 4),
``hw_ver`` (read only, shown as a diagnostic), ``connect_latency``, ``averaging_measurements``, the pseudo-random
advertising delay (low nibble of ``flg3``), ``event_adv_cnt``, and the temperature/humidity offsets (not in this
struct on >= 4.7, see above).
Every write sends the freshly read block back with only the targeted bits/bytes changed, so everything above
is preserved byte for byte.
"""

from __future__ import annotations

from dataclasses import dataclass

CMD_CFG = 0x55
# Firmware versions whose layout was compared (docs/version-layouts.md): 4.2 .. 5.5 (every tag) and 5.7 .. 5.9. 4.1 and older are
# refused (flg2 bits differ, the reply can be stale), 5.6 only existed as beta commits and was not compared.
VERIFIED_VERSIONS = frozenset(
    {0x42, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x50, 0x51, 0x52, 0x53, 0x54, 0x55, 0x57, 0x58, 0x59}
)
# Fields that do not exist (the bit is reserved or unused by every display driver) below a version: (first version with it, keys)
VERSION_EXCLUDED: tuple[tuple[int, frozenset[str]], ...] = (
    (0x43, frozenset({"screen_off"})),  # flg2 bit 7 is reserved in 4.2
    (0x49, frozenset({"not_day_of_week"})),  # flg3 is unused before 4.9 (weekday: MASK_FLG3_WEEKDAY from 4.9, same bit)
    (0x54, frozenset({"date_ddmm"})),  # flg3 bit 6 is reserved in 5.1 .. 5.3 (and flg3 is unused before)
)
MIN_LEN = 12  # 0x55, version, flg, flg2, flg3, event_adv_cnt, adv, meas, rf, latency, step, hw_ver


class ConfigError(Exception):
    """The block cannot be read or changed safely."""


@dataclass(frozen=True)
class Field:
    """One exposed setting inside the reply (byte index counts from the 0x55 byte)."""

    key: str
    index: int
    mask: int = 0xFF
    display: bool = False  # board specific display bit: only usable where DISPLAY_BY_HW lists it

    @property
    def shift(self) -> int:
        return (self.mask & -self.mask).bit_length() - 1


FLG, FLG2, FLG3, ADV, MEAS, RF = 2, 3, 4, 6, 7, 8
FIELDS: dict[str, Field] = {
    f.key: f
    for f in (
        Field("adv_interval", ADV),  # x62.5 ms, 1..160
        Field("measure_interval", MEAS),  # x adv interval, 2..25
        Field("rf_tx_power", RF),
        Field("adv_type", FLG, 0x03),  # 0 atc1441, 1 custom (pvvx), 2 mi, 3 BTHome
        Field("adv_crypto", FLG2, 0x08),  # protected: only with a key, via set_encryption
        Field("bt5phy", FLG2, 0x20),
        Field("longrange", FLG2, 0x40),
        # display bits, see DISPLAY_BY_HW for which board may use which key
        Field("comfort_smiley", FLG, 0x04, display=True),
        Field("x100", FLG, 0x08, display=True),  # MJWSD05MMC: temperature/humidity with two decimals
        Field("temp_f", FLG, 0x10, display=True),  # 1 = Fahrenheit
        Field("show_battery", FLG, 0x20, display=True),  # LYWSD03MMC (same bit is time_am_pm on MJWSD05MMC)
        Field("time_am_pm", FLG, 0x20, display=True),  # MJWSD05MMC: 12 hour clock
        Field("smiley", FLG2, 0x07, display=True),  # LYWSD03MMC (same bits are screen_type on MJWSD05MMC)
        Field("screen_type", FLG2, 0x07, display=True),  # MJWSD05MMC: 0 time, 1 temp, 2 humi, 3 batt %, 4 batt V, 5 ext
        Field("screen_off", FLG2, 0x80, display=True),
        Field("date_ddmm", FLG3, 0x40, display=True),  # MJWSD05MMC (en): 1 = DD/MM, 0 = MM/DD
        Field("not_day_of_week", FLG3, 0x80, display=True),  # MJWSD05MMC: 1 = weekday hidden (inverted!)
    )
}
_LYWSD03MMC_DISPLAY = frozenset({"comfort_smiley", "temp_f", "show_battery", "smiley", "screen_off"})
_MJWSD05MMC_DISPLAY = frozenset(
    {"comfort_smiley", "x100", "temp_f", "time_am_pm", "screen_type", "screen_off", "not_day_of_week"}
)
# Hardware id -> display keys (see docs/display-layouts.md for the source of each entry). Everything else gets none:
# LYWSD02MMC uses the bits differently and has no screen at all on the other boards (TS0201, ...). The other display boards
# (e-ink and LCD, other glyph sets) only get the two bits whose meaning is the same everywhere: unit and screen off.
_UNIT_SCREEN_ONLY = frozenset({"temp_f", "screen_off"})
DISPLAY_BY_HW: dict[int, frozenset[str]] = {
    **dict.fromkeys(
        (0, 3, 4, 5, 10, 14), _LYWSD03MMC_DISPLAY
    ),  # DEVICE_LYWSD03MMC (B1.4, B1.9, B1.6, B1.7, B1.5, 2025)
    **dict.fromkeys(
        (1, 2, 6, 7, 8, 11, 13), _UNIT_SCREEN_ONLY
    ),  # MHO-C401, CGG1, CGDK2, CGG1 2022, MHO-C401 2022, MHO-C122, MJWSD06MMC
    9: _MJWSD05MMC_DISPLAY,  # MJWSD05MMC (ch)
    12: _MJWSD05MMC_DISPLAY | {"date_ddmm"},  # MJWSD05MMC (en)
}
PROTECTED = frozenset({"adv_crypto"})  # a write without a matching bindkey would make the sensor unreadable by HA
ADV_TYPES = ["ATC1441", "pvvx custom", "Mi", "BTHome"]
LIMITS = {
    "adv_type": (0, 3),
    "adv_interval": (1, 160),
    "measure_interval": (2, 25),
    "smiley": (0, 7),
    "screen_type": (0, 5),  # SCR_TYPE_ENUM in lcd.h, 6 and 7 do not exist
}

# RF TX power subset (raw byte -> dBm), from the firmware's RF_POWER_* table
TX_POWER: dict[int, float] = {
    63: 10.46,
    41: 7.79,
    29: 5.13,
    191: 3.01,
    169: 0.04,  # firmware default
    156: -3.03,
    140: -9.89,
    134: -15.88,
    130: -25.18,
}
SCREEN_TYPES = ["time", "temperature", "humidity", "battery %", "battery V", "external"]
SMILEYS = ["off", "happy", "sad", "ooo", "frame off", "frame happy", "frame sad", "frame ooo"]


def version_of(raw: bytes) -> int | None:
    return raw[1] if len(raw) >= 2 and raw[0] == CMD_CFG else None


def is_verified(raw: bytes | None) -> bool:
    """True if this block's layout was compared against the firmware source."""
    return raw is not None and len(raw) >= MIN_LEN and version_of(raw) in VERIFIED_VERSIONS


def capabilities(raw: bytes | None, hw_id: int | None) -> set[str]:
    """Field keys that can be used with this firmware version/board."""
    if raw is None or not is_verified(raw):
        return set()
    return capabilities_for(version_of(raw), hw_id)


def capabilities_for(version: int | None, hw_id: int | None) -> set[str]:
    """Field keys for a firmware version byte and hardware id, without a block (what an entry stores): empty for a version
    whose layout was not verified."""
    if version not in VERIFIED_VERSIONS:
        return set()
    display = DISPLAY_BY_HW.get(hw_id, frozenset()) if hw_id is not None else frozenset()
    ver = version or 0
    missing = {k for first, keys in VERSION_EXCLUDED if ver < first for k in keys}
    return {k for k, f in FIELDS.items() if (not f.display or k in display) and k not in missing}


def get(raw: bytes, key: str) -> int:
    f = FIELDS[key]
    return (raw[f.index] & f.mask) >> f.shift


def require_verified(raw: bytes) -> None:
    """Raise unless this firmware's layout/commands were checked (used by every write path)."""
    if not is_verified(raw):
        apply(raw, {}, None)  # raises the right ConfigError


def snapshot(raw: bytes, hw_id: int | None) -> dict[str, int]:
    """Values of every usable field (what a later restore may write back)."""
    return {k: get(raw, k) for k in sorted(capabilities(raw, hw_id) - PROTECTED)}  # never the encryption flag


def hw_ver(raw: bytes) -> int | None:
    """Read-only hardware version byte of the block."""
    return raw[11] if len(raw) >= MIN_LEN else None


def apply(
    raw: bytes, changes: dict[str, int], hw_id: int | None, allow_protected: frozenset[str] = frozenset()
) -> bytes:
    """Return the payload (cfg bytes, no 0x55/version) = raw with only ``changes`` applied."""
    if not is_verified(raw):
        ver = version_of(raw)
        raise ConfigError(
            f"firmware {ver >> 4}.{ver & 15} is not verified for configuration changes"
            if ver is not None
            else "unrecognised configuration reply"
        )
    allowed = capabilities(raw, hw_id)
    out = bytearray(raw)
    for key, value in changes.items():
        if key not in allowed:
            raise ConfigError(f"{key} cannot be changed on this sensor")
        if key in PROTECTED and key not in allow_protected:
            raise ConfigError(f"{key} can only be changed with the set_encryption action")
        f = FIELDS[key]
        lo, hi = LIMITS.get(key, (0, f.mask >> f.shift))
        if not lo <= value <= hi:
            raise ConfigError(f"{key}={value} is outside {lo}..{hi}")
        out[f.index] = (out[f.index] & ~f.mask & 0xFF) | ((value << f.shift) & f.mask)
    return bytes(out[2:])
