"""Device clock: command 0x23 (local wall-clock seconds, like TelinkMiFlasher's Set Time)."""

import time
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.pvvx_ota import clock as clk
from custom_components.pvvx_ota.const import CONF_AUTO_CLOCK_SYNC
from custom_components.pvvx_ota.diagnostics import async_get_config_entry_diagnostics as get_diagnostics

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


async def test_clock_read_and_last_set_are_logged(hass: HomeAssistant, enable_bluetooth, freezer, caplog):
    caplog.set_level("DEBUG", logger="custom_components.pvvx_ota")
    await berlin(hass, freezer)
    client = FakeClient(cfg=SAMPLE)
    client.last_set = 0
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        assert f"sensor time 0, last set never, offset {-TARGET} s" in caplog.text
        assert f"clock set (it was {-TARGET} s off; sensor last set: never)" in caplog.text
        assert "restarted" not in caplog.text  # this run had not set it before
        assert entry.runtime_data.last_clock_read["offset_s"] == -TARGET
        assert client.time_reads == 1  # the read only, no read-back
    finally:
        [c.stop() for c in ctx]


async def test_restart_since_the_last_set_is_reported(hass: HomeAssistant, enable_bluetooth, freezer, caplog):
    await berlin(hass, freezer)
    client = FakeClient(cfg=SAMPLE)
    client.last_set = 0
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        assert client.last_set == TARGET
        client.reboot()
        caplog.clear()
        entity = hass.data["entity_components"]["update"].get_entity("update.atc_000001")
        await entity._async_read_device()
        assert "sensor last set: never; sensor restarted since the last set" in caplog.text
    finally:
        [c.stop() for c in ctx]


async def test_four_byte_reply_reports_not_reported(hass: HomeAssistant, enable_bluetooth, freezer, caplog):
    await berlin(hass, freezer)
    client = FakeClient(cfg=SAMPLE)  # no last_set field
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        assert "sensor last set: not reported)" in caplog.text
        assert entry.runtime_data.last_clock_read["last_set"] is None
        assert "did not keep" not in caplog.text
    finally:
        [c.stop() for c in ctx]


async def test_a_clock_that_stands_still_while_connected_is_not_a_warning(
    hass: HomeAssistant, enable_bluetooth, freezer, caplog
):
    """Observed on real sensors: the clock does not advance during a connection. No read-back, so nothing to warn about."""
    caplog.set_level("DEBUG", logger="custom_components.pvvx_ota")
    await berlin(hass, freezer)
    client = FakeClient(cfg=SAMPLE)
    client.clock_frozen_while_connected = True
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        assert client.time_writes == [clk.build_set(TARGET)] and client.time_reads == 1
        assert not [r for r in caplog.records if r.name.startswith("custom_components.pvvx_ota") and r.levelno >= 30]
        assert "read back" not in caplog.text and "did not keep" not in caplog.text
    finally:
        [c.stop() for c in ctx]


def test_the_fake_clock_stands_still_while_connected():
    now = [1000.0]
    client = FakeClient(cfg=SAMPLE)
    client.time_source = lambda: now[0]
    client.clock_frozen_while_connected = True
    client.device_time = 500
    now[0] += 10  # between connections: ticks
    assert client.device_time == 510
    client._session_at = now[0]  # a connection opens
    now[0] += 6
    assert client.device_time == 510  # frozen while connected
    client._lost += now[0] - client._session_at  # what disconnect() does
    client._session_at = None
    assert client.device_time == 510
    now[0] += 4
    assert client.device_time == 514


# ---- drift calibration -------------------------------------------------------------------------------------------


def test_drift_arithmetic():
    reading = clk.DeviceTime(1_000_000 + 86400 + 4, 1_000_000)  # 4 s fast after exactly a day
    d = clk.measure_drift(reading, 1_000_000, 86400.0)
    assert d is not None and d.problem is None
    assert round(d.ppm, 2) == 46.30
    assert clk.corrected_step(16_000_000, d) == 16_000_741  # +46.3 ppm = 741 ticks (16 per ppm): slower
    assert clk.build_step_write(16_000_741) == bytes([0x24]) + (741).to_bytes(2, "little", signed=True)
    assert clk.build_step_write(15_999_000) == bytes([0x24]) + (-1000).to_bytes(2, "little", signed=True)
    assert clk.parse_step(bytes([0x24]) + (16_000_741).to_bytes(4, "little")) == 16_000_741
    for bad in (b"", b"\x24\x00\x00", b"\x23" + bytes(4)):
        with pytest.raises(clk.ClockError):
            clk.parse_step(bad)
    for step in (clk.MIN_STEP - 1, clk.MAX_STEP + 1):
        with pytest.raises(clk.ClockError):
            clk.build_step_write(step)
    # not comparable: other baseline, no last_set field, too young
    assert clk.measure_drift(clk.DeviceTime(5, 0), 1_000_000, 86400.0) is None
    assert clk.measure_drift(clk.DeviceTime(5, None), 1_000_000, 86400.0) is None
    assert clk.measure_drift(reading, 1_000_000, 86399.0) is None
    assert (
        clk.measure_drift(clk.DeviceTime(1_000_000 + 86401, 1_000_000), 1_000_000, 86400.0).problem
        == "offset too small"
    )
    assert (
        clk.measure_drift(clk.DeviceTime(1_000_000 + 86400 + 100, 1_000_000), 1_000_000, 86400.0).problem
        == "implausible"
    )
    assert clk.corrected_step(clk.MAX_STEP - 5, clk.Drift(86400.0, 40.0)) == clk.MAX_STEP  # clamped


class Drifting:
    """A sensor whose crystal runs fast, with a host clock the test moves (the fake follows it)."""

    def __init__(self, ppm=50.0, frozen=False, start=None):
        self.now = [NOW_UTC.timestamp() if start is None else start]
        self.client = FakeClient(cfg=SAMPLE)
        self.client.time_source = lambda: self.now[0]
        self.client.last_set = 0
        self.client.crystal_ppm = ppm
        self.client.clock_frozen_while_connected = frozen

    async def read(self, hass, freezer, hours):
        """Move the host ``hours`` on and run a connection that reads the clock (the version read). Only the integration's
        view of the time moves: shifting Home Assistant's own clock would fire its timers (catalog retries)."""
        from homeassistant.util import dt as dt_util

        self.now[0] += hours * 3600
        moved = datetime.fromtimestamp(self.now[0], UTC)
        entity = hass.data["entity_components"]["update"].get_entity("update.atc_000001")
        with (
            patch.object(dt_util, "utcnow", lambda: moved),
            patch.object(dt_util, "now", lambda time_zone=None: moved.astimezone(dt_util.get_default_time_zone())),
        ):
            await entity._async_read_device()


async def finish(hass, entry, ctx):
    await hass.config_entries.async_unload(
        entry.entry_id
    )  # the update entity's catalog retry timer would outlive the test
    await hass.async_block_till_done()
    [c.stop() for c in ctx]


async def drift_setup(hass, freezer, **kw):
    await berlin(hass, freezer)
    sim = Drifting(**kw)
    entry, calls, ctx = await setup_cfg_entry(hass, sim.client)
    return sim, entry, ctx


async def test_no_adjustment_before_24_hours(hass: HomeAssistant, enable_bluetooth, freezer):
    sim, entry, ctx = await drift_setup(hass, freezer, ppm=500.0)  # 11 s off after 6 h
    try:
        await sim.read(hass, freezer, 6)
        assert sim.client.step_commands == [] and sim.client.tick_step == 16_000_000
        assert entry.runtime_data.drift.last is None
    finally:
        await finish(hass, entry, ctx)


async def test_a_measured_drift_adjusts_the_tick_step(hass: HomeAssistant, enable_bluetooth, freezer, caplog):
    caplog.set_level("INFO", logger="custom_components.pvvx_ota")
    sim, entry, ctx = await drift_setup(hass, freezer, ppm=50.0)  # +50 ppm: 4.3 s fast per day
    try:
        await sim.read(hass, freezer, 25)  # the sensor is 4.5 s ahead: within the tolerance, so only the step changes
        # 16 ticks per ppm: 50 ppm = 800 ticks (the integer second readings may be a second off)
        assert 700 <= sim.client.tick_step - 16_000_000 <= 900
        assert [c[0] for c in sim.client.step_commands] == [0x24, 0x24, 0x24]  # read, write, read-back
        assert "clock drift 4" in caplog.text and "ppm over 25.0 h, tick step 16000000 -> 160" in caplog.text
        last = entry.runtime_data.drift.last
        assert (
            last["result"] == "adjusted" and last["old_step"] == 16_000_000 and last["new_step"] == sim.client.tick_step
        )
        assert last["elapsed_s"] == 25 * 3600
        assert sim.client.time_writes[-1] == clk.build_set(TARGET + 25 * 3600)  # re-baselined for the new rate
        # a day later the clock keeps time
        await sim.read(hass, freezer, 25)
        assert abs(sim.client.device_time - (TARGET + 50 * 3600)) <= 1 or sim.client.time_writes
        assert entry.runtime_data.drift.last["result"] in ("offset too small", "adjusted")
        data = (await get_diagnostics(hass, entry))["clock_drift"]
        assert data["result"] == entry.runtime_data.drift.last["result"]
    finally:
        await finish(hass, entry, ctx)


async def test_the_second_day_is_exact(hass: HomeAssistant, enable_bluetooth, freezer):
    sim, entry, ctx = await drift_setup(hass, freezer, ppm=50.0)
    try:
        await sim.read(hass, freezer, 24)
        step = sim.client.tick_step
        assert step != 16_000_000
        n = len(sim.client.time_writes)
        await sim.read(hass, freezer, 24)  # corrected: 0 or 1 s off, nothing to do
        assert sim.client.tick_step == step and len(sim.client.time_writes) == n
        assert entry.runtime_data.drift.last["result"] == "offset too small"
    finally:
        await finish(hass, entry, ctx)


async def test_no_adjustment_after_a_restart_or_a_foreign_set(hass: HomeAssistant, enable_bluetooth, freezer):
    sim, entry, ctx = await drift_setup(hass, freezer, ppm=50.0)
    try:
        sim.client.reboot(stale=sim.client.device_time - 1000)  # restart: last_set 0, clock stale
        await sim.read(hass, freezer, 30)
        assert sim.client.step_commands == []
        assert entry.runtime_data.drift.last is None
        # the integration set it again at that read; now somebody else sets the clock
        sim.client.last_set = sim.client.last_set + 7  # a mismatching "last set"
        await sim.read(hass, freezer, 30)
        assert sim.client.step_commands == []
    finally:
        await finish(hass, entry, ctx)


async def test_an_implausible_drift_is_ignored(hass: HomeAssistant, enable_bluetooth, freezer):
    sim, entry, ctx = await drift_setup(hass, freezer, ppm=2000.0)
    try:
        await sim.read(hass, freezer, 30)
        assert sim.client.step_commands == [] and entry.runtime_data.drift.last["result"] == "implausible"
    finally:
        await finish(hass, entry, ctx)


async def test_the_step_is_clamped_to_the_command_range(hass: HomeAssistant, enable_bluetooth, freezer):
    sim, entry, ctx = await drift_setup(hass, freezer, ppm=2117.0)
    try:
        sim.client.tick_step = clk.MAX_STEP - 500  # near the end of the range; net +100 ppm, which needs 1600 ticks
        await sim.read(hass, freezer, 30)
        assert sim.client.tick_step == clk.MAX_STEP
        assert sim.client.step_commands[1] == clk.build_step_write(clk.MAX_STEP)
    finally:
        await finish(hass, entry, ctx)


async def test_a_sensor_without_the_command_is_not_asked(hass: HomeAssistant, enable_bluetooth, freezer):
    sim, entry, ctx = await drift_setup(hass, freezer, ppm=50.0)
    try:
        sim.client.last_set = None  # 4 byte clock replies: no TIME_ADJUST in this build
        await sim.read(hass, freezer, 30)
        assert sim.client.step_commands == [] and entry.runtime_data.drift.last is None
    finally:
        await finish(hass, entry, ctx)


async def test_the_option_switches_the_calibration_off(hass: HomeAssistant, enable_bluetooth, freezer):
    await berlin(hass, freezer)
    sim = Drifting(ppm=50.0)
    entry, calls, ctx = await setup_cfg_entry(hass, sim.client, options={CONF_AUTO_CLOCK_SYNC: False})
    try:
        await sim.read(hass, freezer, 30)
        assert sim.client.step_commands == []
    finally:
        await finish(hass, entry, ctx)


async def test_a_read_back_mismatch_is_logged_and_not_retried(hass: HomeAssistant, enable_bluetooth, freezer, caplog):
    sim, entry, ctx = await drift_setup(hass, freezer, ppm=50.0)
    try:
        sim.client.step_ignored = True  # answered, not kept
        await sim.read(hass, freezer, 25)
        assert sim.client.tick_step == 16_000_000
        assert "not retried" in caplog.text and entry.runtime_data.drift.last["result"] == "read-back mismatch"
        assert [c for c in caplog.records if c.levelno >= 30 and "tick step" in c.getMessage()]
        n = len(sim.client.step_commands)
        await sim.read(hass, freezer, 1)  # same day: no second attempt
        assert len(sim.client.step_commands) == n and entry.runtime_data.drift.last["result"] == "rate limited"
    finally:
        await finish(hass, entry, ctx)


async def test_no_answer_to_the_step_command_is_not_fatal(hass: HomeAssistant, enable_bluetooth):
    # real time: the reply timeout must really elapse
    sim = Drifting(ppm=50.0, start=time.time())
    entry, calls, ctx = await setup_cfg_entry(hass, sim.client)
    try:
        sim.client.step_no_reply = True
        with patch("custom_components.pvvx_ota.drift.TIMEOUT", 0.05):
            await sim.read(hass, None, 25)
        assert entry.runtime_data.drift.last["result"].startswith("failed")
        assert hass.states.get(ent(hass, "number", "adv_interval")).state == "1.0"
    finally:
        await finish(hass, entry, ctx)


async def test_a_step_outside_the_range_is_left_alone(hass: HomeAssistant, enable_bluetooth, freezer):
    sim, entry, ctx = await drift_setup(hass, freezer, ppm=50.0)
    try:
        sim.client.tick_step = 24_000_000  # not a 16 MHz timer
        sim.client.crystal_ppm = 500_075.0  # a 24 MHz timer 50 ppm fast, so the drift itself is plausible
        await sim.read(hass, freezer, 25)
        assert len(sim.client.step_commands) == 1 and entry.runtime_data.drift.last["result"] == "unexpected step"
    finally:
        await finish(hass, entry, ctx)


async def test_a_step_already_at_the_limit_is_not_written(hass: HomeAssistant, enable_bluetooth, freezer):
    sim, entry, ctx = await drift_setup(hass, freezer, ppm=2147.0)
    try:
        sim.client.tick_step = clk.MAX_STEP  # the clock still runs 100 ppm fast, nothing left to correct with
        await sim.read(hass, freezer, 30)
        assert [len(c) for c in sim.client.step_commands] == [1]  # the read only
        assert entry.runtime_data.drift.last["result"] == "no change"
    finally:
        await finish(hass, entry, ctx)


async def test_a_step_stored_as_something_else_starts_a_new_baseline(
    hass: HomeAssistant, enable_bluetooth, freezer, caplog
):
    sim, entry, ctx = await drift_setup(hass, freezer, ppm=50.0)
    try:
        sim.client.step_stores = 16_000_123
        await sim.read(hass, freezer, 25)
        assert "not retried" in caplog.text and entry.runtime_data.drift.last["result"] == "read-back mismatch"
        assert sim.client.time_writes[-1] == clk.build_set(TARGET + 25 * 3600)  # the rate changed: set the clock again
    finally:
        await finish(hass, entry, ctx)
