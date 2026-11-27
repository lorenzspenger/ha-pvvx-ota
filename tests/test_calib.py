"""Calibration offsets: command 0x25 coefficients."""

import struct

import pytest

from custom_components.pvvx_ota import calib

# Tk 17500, Hk 10000, Tz -4500 (-45.00), Hz 0, id 0x12345678, i2c 0x70, SHTC3
REPLY = b"\x25" + struct.pack("<IIhhIBB", 17500, 10000, -4500, 0, 0x12345678, 0x70, 1)


def test_parse_known_sample():
    c = calib.parse(REPLY)
    assert (c.tk, c.hk, c.tz, c.hz, c.sensor_type, c.sensor_id) == (17500, 10000, -4500, 0, 1, 0x12345678)
    assert c.known and c.temp_offset == 0.0 and c.humi_offset == 0.0
    assert len(REPLY) == calib.REPLY_LEN and c.raw == REPLY[1:13]


def test_offsets_are_relative_to_the_chip_factory_values():
    sht4x = calib.parse(b"\x25" + struct.pack("<IIhhIBB", 17500, 12500, -4500 + 50, -600 - 120, 1, 0x44, 2))
    assert sht4x.temp_offset == 0.5 and sht4x.humi_offset == -1.2  # not -44.5 / -7.2


def test_write_changes_only_tz_hz_and_keeps_the_rest():
    c = calib.parse(REPLY)
    req = calib.build_write(c, temp_offset=0.7)
    assert req[0] == 0x25 and len(req) == 13
    tk, hk, tz, hz = struct.unpack("<IIhh", req[1:])
    assert (tk, hk, tz, hz) == (17500, 10000, -4500 + 70, 0)  # Tz moved by +0.70, the rest as read
    req = calib.build_write(c, humi_offset=-2.5)
    assert struct.unpack("<IIhh", req[1:]) == (17500, 10000, -4500, -250)
    both = calib.build_write(c, temp_offset=-1.0, humi_offset=3.0)
    assert struct.unpack("<IIhh", both[1:]) == (17500, 10000, -4600, 300)


def test_slopes_are_never_touched_even_if_unusual():
    c = calib.parse(b"\x25" + struct.pack("<IIhhIBB", 17321, 9876, -4500, 0, 1, 0x70, 1))
    tk, hk, _, _ = struct.unpack("<IIhh", calib.build_write(c, temp_offset=1.0)[1:])
    assert (tk, hk) == (17321, 9876)


@pytest.mark.parametrize("value", [12.6, -12.6, 100])
def test_out_of_range_refused(value):
    with pytest.raises(calib.CalibError, match="outside"):
        calib.build_write(calib.parse(REPLY), temp_offset=value)


def test_unknown_chip_type_offers_nothing():
    c = calib.parse(b"\x25" + struct.pack("<IIhhIBB", 1, 1, 0, 0, 1, 0x70, 99))
    assert not c.known and c.temp_offset is None and c.humi_offset is None
    with pytest.raises(calib.CalibError, match="not known"):
        calib.build_write(c, temp_offset=1.0)


@pytest.mark.parametrize("bad", [b"", b"\x25" + bytes(5), b"\x55" + bytes(18)])
def test_garbled_reply_refused(bad):
    with pytest.raises(calib.CalibError):
        calib.parse(bad)


# -- entities -----------------------------------------------------------------------------------
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402

from custom_components.pvvx_ota.const import DOMAIN  # noqa: E402

from .fake_gatt import FakeClient  # noqa: E402
from .test_config import SAMPLE, ent, setup_cfg_entry  # noqa: E402
from .translated import raises_key  # noqa: E402


async def enable(hass, eid):
    er.async_get(hass).async_update_entity(eid, disabled_by=None)
    await hass.config_entries.async_reload(hass.config_entries.async_entries(DOMAIN)[0].entry_id)
    await hass.async_block_till_done()


async def test_offset_entities_disabled_by_default_and_read_in_the_setup_connection(
    hass: HomeAssistant, enable_bluetooth
):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        assert len(calls) == 1  # the coefficients came in the connection of the setup read: no extra connection
        for key in ("temp_offset", "humi_offset"):
            eid = ent(hass, "number", key)
            reg = er.async_get(hass).async_get(eid)
            assert reg.entity_category == "config" and reg.disabled_by == er.RegistryEntryDisabler.INTEGRATION
        await enable(hass, ent(hass, "number", "temp_offset"))
        eid = ent(hass, "number", "temp_offset")
        assert hass.states.get(eid).state == "0.0"
        assert "factory value" in hass.states.get(eid).attributes["warning"]
    finally:
        [c.stop() for c in ctx]


async def test_set_offset_is_read_modify_write_and_verified(hass: HomeAssistant, enable_bluetooth):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        await enable(hass, ent(hass, "number", "temp_offset"))
        # someone changed the slope in TelinkMiFlasher meanwhile: it must survive our write
        client.coef = struct.pack("<IIhhIBB", 17321, 9876, -4500, 0, 0x12345678, 0x70, 1)
        eid = ent(hass, "number", "temp_offset")
        await hass.services.async_call("number", "set_value", {"entity_id": eid, "value": 1.5}, blocking=True)
        assert len(client.coef_writes) == 1 and client.coef_writes[0][0] == 0x25 and len(client.coef_writes[0]) == 13
        tk, hk, tz, hz = struct.unpack("<IIhh", client.coef_writes[0][1:])
        assert (tk, hk, tz, hz) == (17321, 9876, -4500 + 150, 0)
        assert hass.states.get(eid).state == "1.5"
        assert client.cfg_writes == []  # the 0x55 config block was not touched
    finally:
        [c.stop() for c in ctx]


async def test_offset_not_offered_for_unknown_chip_or_unverified_firmware(hass: HomeAssistant, enable_bluetooth):
    client = FakeClient(cfg=SAMPLE)
    client.coef = struct.pack("<IIhhIBB", 1, 1, 0, 0, 1, 0x70, 99)  # chip type we have no factory values for
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        # the chip type is only known after the first read: the entities exist until then, but stay unavailable...
        assert hass.config_entries.async_get_entry(entry.entry_id).data["calib_known"] is False
        eid = ent(hass, "number", "temp_offset")
        assert eid is None or hass.states.get(eid) is None  # (disabled by default: never live)
        # ...and the next setup removes them
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert ent(hass, "number", "temp_offset") is None and ent(hass, "number", "humi_offset") is None
    finally:
        [c.stop() for c in ctx]


async def test_no_offsets_on_unverified_firmware(hass: HomeAssistant, enable_bluetooth):
    old = FakeClient(
        cfg=bytes([0x55, 0x5A]) + SAMPLE[2:]
    )  # 5.10: layout not verified, so the coefficients are not read
    entry, calls, ctx = await setup_cfg_entry(hass, old, version=0x5A)
    try:
        assert ent(hass, "number", "temp_offset") is None
        assert entry.runtime_data.calib is None
    finally:
        [c.stop() for c in ctx]


async def test_failed_verification_is_reported(hass: HomeAssistant, enable_bluetooth):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        store = entry.runtime_data
        orig = client.write_gatt_char

        async def clamp(char, data, response):
            if data[:1] == b"\x25" and len(data) > 1:  # the sensor stores something else than asked
                data = data[:9] + struct.pack("<h", -4500) + data[11:]
            return await orig(char, data, response)

        client.write_gatt_char = clamp
        with raises_key("temperature_offset_mismatch"):
            await store.async_set_calibration(temp_offset=2.0)
    finally:
        [c.stop() for c in ctx]
