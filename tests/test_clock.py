"""Device clock: command 0x23 (local wall-clock seconds, like TelinkMiFlasher's Set Time)."""

from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.pvvx_ota import clock as clk
from custom_components.pvvx_ota.const import CONF_AUTO_CLOCK_SYNC

from .fake_gatt import FakeClient
from .test_config import SAMPLE, ent, setup_cfg_entry

NOW_UTC = datetime(2026, 11, 23, 11, 0, 0, tzinfo=UTC)  # 12:00:00 in Berlin (CET, +1 h)
TARGET = int(NOW_UTC.timestamp()) + 3600


def test_local_epoch_adds_the_utc_offset():
    berlin_winter = NOW_UTC.astimezone(timezone(timedelta(hours=1)))
    berlin_summer = datetime(2026, 7, 1, 10, 0, 0, tzinfo=UTC).astimezone(timezone(timedelta(hours=2)))
    assert clk.local_epoch(berlin_winter) == int(NOW_UTC.timestamp()) + 3600
    assert clk.local_epoch(berlin_summer) == int(datetime(2026, 7, 1, 10, tzinfo=UTC).timestamp()) + 7200
    assert clk.local_epoch(NOW_UTC) == int(NOW_UTC.timestamp())  # UTC: no shift
    with pytest.raises(clk.ClockError):
        clk.local_epoch(datetime(2026, 1, 1))  # naive


def test_build_and_parse_bytes():
    assert clk.build_set(0x01020304) == bytes([0x23, 0x04, 0x03, 0x02, 0x01])  # little endian, as the web flasher
    assert clk.parse(bytes([0x23, 1, 0, 0, 0])) == clk.DeviceTime(1, None)
    assert clk.parse(bytes([0x23, 1, 0, 0, 0, 2, 0, 0, 0])) == clk.DeviceTime(1, 2)  # with the "last set" time
    for bad in (b"", b"\x23\x01", b"\x55" + bytes(8)):
        with pytest.raises(clk.ClockError):
            clk.parse(bad)
    for bad_time in (0, 2**32):
        with pytest.raises(clk.ClockError):
            clk.build_set(bad_time)


async def berlin(hass, freezer):
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to(NOW_UTC)


async def test_setup_sets_a_clock_that_is_far_off_in_the_same_connection(
    hass: HomeAssistant, enable_bluetooth, freezer
):
    await berlin(hass, freezer)
    client = FakeClient(cfg=SAMPLE)  # device time 0 (battery was changed)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        assert len(calls) == 1  # no extra connection for it
        assert client.time_writes == [clk.build_set(TARGET)]
        assert client.device_time == TARGET
        assert entry.runtime_data.last_clock["was_off_s"] == -TARGET
    finally:
        [c.stop() for c in ctx]


async def test_a_clock_within_tolerance_is_left_alone(hass: HomeAssistant, enable_bluetooth, freezer):
    await berlin(hass, freezer)
    client = FakeClient(cfg=SAMPLE)
    client.device_time = TARGET - 3
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        assert client.time_writes == []
    finally:
        [c.stop() for c in ctx]


async def test_auto_sync_can_be_switched_off(hass: HomeAssistant, enable_bluetooth, freezer):
    await berlin(hass, freezer)
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client, options={CONF_AUTO_CLOCK_SYNC: False})
    try:
        assert client.time_writes == []
    finally:
        [c.stop() for c in ctx]


async def test_clock_failure_never_breaks_the_connection(hass: HomeAssistant, enable_bluetooth):
    # no frozen time here: the reply timeout must really elapse
    client = FakeClient(cfg=SAMPLE)
    client.no_clock_reply = True
    with patch("custom_components.pvvx_ota.store.CLOCK_TIMEOUT", 0.05):
        entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        assert hass.states.get(ent(hass, "number", "adv_interval")).state == "1.0"  # set up normally
    finally:
        [c.stop() for c in ctx]


async def test_sync_clock_button_always_sets_it(hass: HomeAssistant, enable_bluetooth, freezer):
    await berlin(hass, freezer)
    client = FakeClient(cfg=SAMPLE)
    client.device_time = TARGET  # already right
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        assert client.time_writes == []
        await hass.services.async_call(
            "button", "press", {"entity_id": ent(hass, "button", "sync_clock")}, blocking=True
        )
        assert client.time_writes == [clk.build_set(TARGET)]  # forced
    finally:
        [c.stop() for c in ctx]


async def test_reboot_and_flash_connections_sync_too(hass: HomeAssistant, enable_bluetooth):
    # real time (the flash path has real sleeps): compare the written time with "now" within a few seconds
    from homeassistant.util import dt as dt_util

    await hass.config.async_set_time_zone("Europe/Berlin")
    client = FakeClient(cfg=SAMPLE[:11] + bytes([0, 0]))
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)

    def written(i):
        return int.from_bytes(client.time_writes[i][1:5], "little")

    try:
        client.device_time = 0  # the clock was lost again
        store = entry.runtime_data
        with patch("custom_components.pvvx_ota.store.REBOOT_WATCH", 0.0):
            await store.async_reboot()
        assert abs(written(-1) - clk.local_epoch(dt_util.now())) <= 5 and client.reboots == 1
        n = len(client.time_writes)
        client.device_time = 0
        with patch("custom_components.pvvx_ota.update.async_call_later"):
            await hass.services.async_call(
                "pvvx_ota", "flash_latest", {"entity_id": "update.atc_000001"}, blocking=True
            )
        assert len(client.time_writes) == n + 1 and client.writes  # synced in the flash's pre-read, then flashed
    finally:
        [c.stop() for c in ctx]


async def test_unverified_firmware_gets_no_clock_writes(hass: HomeAssistant, enable_bluetooth, freezer):
    await berlin(hass, freezer)
    old = FakeClient(cfg=bytes([0x55, 0x5A]) + SAMPLE[2:])
    entry, calls, ctx = await setup_cfg_entry(hass, old, version=0x5A)
    try:
        assert old.time_writes == []
    finally:
        [c.stop() for c in ctx]


async def version_read_writes(hass, options=None):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client, options=options)
    try:
        client.time_writes.clear()
        client.device_time = 0  # the clock was lost since the setup read
        entity = hass.data["entity_components"]["update"].get_entity("update.atc_000001")
        await entity._async_read_device()
        return client.time_writes, client
    finally:
        [c.stop() for c in ctx]


async def test_the_version_read_sets_the_clock_in_its_own_connection(hass: HomeAssistant, enable_bluetooth, freezer):
    await berlin(hass, freezer)
    writes, client = await version_read_writes(hass)
    assert writes == [clk.build_set(TARGET)] and client.device_time == TARGET


async def test_the_version_read_leaves_the_clock_alone_when_auto_sync_is_off(
    hass: HomeAssistant, enable_bluetooth, freezer
):
    await berlin(hass, freezer)
    writes, _ = await version_read_writes(hass, options={CONF_AUTO_CLOCK_SYNC: False})
    assert writes == []
