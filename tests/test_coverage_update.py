"""Update entity: error paths and edge cases."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.pvvx_ota import store as store_mod
from custom_components.pvvx_ota import update as upd
from custom_components.pvvx_ota.firmware import Catalog, FirmwareError, FirmwareSource
from custom_components.pvvx_ota.ota import OtaError

from .fake_gatt import FakeClient
from .test_config import SAMPLE, fast_batch, setup_cfg_entry  # noqa: F401
from .test_ha import bthome_device

ENTITY = "update.atc_000001"


@pytest.fixture
async def setup(hass: HomeAssistant, enable_bluetooth, fast_batch):
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    entity = hass.data["entity_components"]["update"].get_entity(ENTITY)
    yield hass, entry, entity
    [c.stop() for c in ctx]


async def test_first_update_survives_a_missing_catalog(setup, caplog):
    hass, entry, entity = setup
    with patch.object(FirmwareSource, "async_catalog", side_effect=FirmwareError("offline")):
        await entity._async_first_update()
    assert "offline" in caplog.text


async def test_unrelated_device_registry_events_do_not_reload(setup):
    hass, entry, entity = setup
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        entity._registry_event(SimpleNamespace(data={"action": "update", "changes": {"name_by_user": "x"}}))
    reload.assert_not_called()


async def test_state_without_a_catalog_claims_nothing(setup):
    hass, entry, entity = setup
    entity._catalog = None
    assert entity.latest_version is None  # unknown, neither up to date nor an update
    assert entity.release_summary is None
    assert await entity.async_release_notes() is None
    assert entity.extra_state_attributes["hardware_id"] == entity._hw_id


async def test_attributes_report_errors_and_unknown_boards(setup):
    hass, entry, entity = setup
    entity._catalog = Catalog(version=89, beta=None, images=("bin/weird.bin",) * 32)
    entity._catalog_error = "not connectable"
    attrs = entity.extra_state_attributes
    assert attrs["board"] is None and attrs["last_error"] == "not connectable"


async def test_poll_reports_catalog_and_connection_problems(setup, caplog):
    hass, entry, entity = setup
    with patch.object(FirmwareSource, "async_catalog", side_effect=FirmwareError("no list")):
        await entity.async_update()
    assert "no list" in caplog.text
    # right after a flash the poll does not connect
    entity._quiet_until = asyncio.get_running_loop().time() + 1000
    with patch.object(upd.PvvxUpdateEntity, "_async_read_device") as read:
        await entity.async_update()
    read.assert_not_called()
    entity._quiet_until = 0
    with patch.object(upd.PvvxUpdateEntity, "_async_read_device", side_effect=HomeAssistantError("out of range")):
        await entity.async_update()
    assert entity._catalog_error == "out of range"
    # a protocol error from the read becomes a Home Assistant error
    with (
        patch.object(upd, "read_cfg_raw", side_effect=OtaError("no answer")),
        pytest.raises(HomeAssistantError, match="no answer"),
    ):
        await entity._async_read_device()
    with pytest.raises(HomeAssistantError, match="unrecognised version reply"):
        entity._store(None, None)


async def test_flash_file_reports_an_unreadable_file(setup, tmp_path):
    hass, entry, entity = setup
    hass.config.allowlist_external_dirs.add(str(tmp_path))
    with pytest.raises(HomeAssistantError, match="cannot read"):
        await entity.async_flash_file(str(tmp_path / "missing.bin"))


async def test_battery_voltage_lookup_edge_cases(setup):
    hass, entry, entity = setup
    registry = entity.registry_entry
    entity.registry_entry = None
    assert entity._battery_voltage() is None  # not registered
    entity.registry_entry = registry
    bt, dev = bthome_device(hass)
    reg = er.async_get(hass)
    reg.async_update_entity(registry.entity_id, device_id=dev.id)
    entity.registry_entry = reg.async_get(registry.entity_id)
    hass.states.async_set(
        reg.async_get_or_create("sensor", "bthome", "temp_uid", config_entry=bt, device_id=dev.id).entity_id, "21.5", {}
    )  # an entity that is not a voltage sensor: skipped
    mv = reg.async_get_or_create(
        "sensor", "bthome", "mv_uid", config_entry=bt, device_id=dev.id, original_device_class="voltage"
    )
    hass.states.async_set(mv.entity_id, "2900", {"unit_of_measurement": "mV", "device_class": "voltage"})
    assert entity._battery_voltage() == pytest.approx(2.9)


async def test_a_device_that_is_not_pvvx_is_not_flashed(setup):
    hass, entry, entity = setup
    with (
        patch.object(upd, "read_cfg_raw", return_value=b"\x00" * 12),
        patch.object(entity, "_check_battery"),
        patch("custom_components.pvvx_ota.update.async_call_later"),
        pytest.raises((HomeAssistantError, FirmwareError), match="not flashing"),
    ):
        await entity.async_flash_latest()


async def test_post_flash_verification_gives_up_after_eight_tries(setup, caplog):
    hass, entry, entity = setup
    captured = []
    with patch.object(upd, "async_call_later", lambda hass_, delay, cb: captured.append(cb) or (lambda: None)):
        entity._schedule_verify()
    caplog.set_level(logging.DEBUG)

    async def no_wait(_):
        return None

    with (
        patch.object(upd.PvvxUpdateEntity, "_async_read_device", side_effect=HomeAssistantError("still rebooting")),
        patch.object(upd.asyncio, "sleep", no_wait),
    ):
        captured[0](None)
        await asyncio.gather(*[t for t in asyncio.all_tasks() if "pvvx_ota_verify" in repr(t)])
    assert "8/8 failed" in caplog.text and "could not confirm the new version" in caplog.text


async def test_missing_catalog_is_retried_soon_and_then_shown(setup):
    """No catalog at startup: the entity says unknown (not 'up to date'), retries after 1, 5, 15 minutes, then waits for the poll."""
    hass, entry, entity = setup
    entity._catalog = None
    delays = []

    def fake_later(hass_, delay, action):
        delays.append((delay, action))
        return lambda: None

    boom = FirmwareError("offline")
    with (
        patch.object(upd, "async_call_later", fake_later),
        patch.object(FirmwareSource, "async_catalog", side_effect=[boom, boom, boom, boom]),
    ):
        await entity._async_first_update()
        assert [d for d, _ in delays] == [60.0] and entity.latest_version is None
        for attempt in range(3):
            await entity._async_catalog_retry(attempt)
        assert [d for d, _ in delays] == [60.0, 300.0, 900.0]  # no 4th retry: the 12 h poll takes over
    cat = Catalog(version=89, beta=None, images=("bin/ATC_v59.bin",) * 32)
    with patch.object(FirmwareSource, "async_catalog", return_value=cat):
        await entity._async_catalog_retry(0)
        assert entity._catalog is cat
        await entity._async_catalog_retry(1)  # already there: nothing more to do
    # the due callback starts the retry as a task
    with patch.object(entity, "_async_catalog_retry") as retry:
        entity._catalog_retry_due(0)
        await hass.async_block_till_done()
    retry.assert_called_once_with(0)


# -- the Bluetooth read interval is an option, apart from the catalog schedule --------------------------------------


def recording_timers():
    """Replace async_track_time_interval: record the intervals, hand out cancel callbacks that note their use."""
    log: list[dict] = []

    def fake(hass, action, interval):
        item = {"action": action, "interval": interval, "cancelled": False}
        log.append(item)
        return lambda: item.update(cancelled=True)

    return log, patch.object(upd, "async_track_time_interval", fake)


async def setup_with_timers(hass, options=None):
    log, patcher = recording_timers()
    with patcher:
        entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE), options=options)
    return entry, ctx, log


async def test_device_poll_defaults_to_three_days_and_the_catalog_keeps_twelve_hours(
    hass, enable_bluetooth, fast_batch
):
    from datetime import timedelta

    entry, ctx, log = await setup_with_timers(hass)
    try:
        assert sorted(item["interval"] for item in log) == [timedelta(hours=12), timedelta(days=3)]
        entity = hass.data["entity_components"]["update"].get_entity(ENTITY)
        assert entity.should_poll is False
    finally:
        [c.stop() for c in ctx]


async def test_changing_the_option_reschedules_without_a_restart(hass, enable_bluetooth, fast_batch):
    from datetime import timedelta

    log, patcher = recording_timers()
    with patcher:
        entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
        try:
            device_timers = [item for item in log if item["interval"] == timedelta(days=3)]
            assert len(device_timers) == 1 and not device_timers[0]["cancelled"]
            hass.config_entries.async_update_entry(entry, options={**entry.options, "device_poll_interval": "1d"})
            await hass.async_block_till_done()
            assert device_timers[0]["cancelled"]  # the old timer is gone
            assert [i for i in log if i["interval"] == timedelta(days=1) and not i["cancelled"]]
            hass.config_entries.async_update_entry(entry, options={**entry.options, "device_poll_interval": "never"})
            await hass.async_block_till_done()
            assert not [i for i in log if i["interval"] == timedelta(days=1) and not i["cancelled"]]
        finally:
            [c.stop() for c in ctx]


async def test_never_schedules_no_device_poll(hass, enable_bluetooth, fast_batch):
    from datetime import timedelta

    entry, ctx, log = await setup_with_timers(hass, options={"device_poll_interval": "never"})
    try:
        assert [item["interval"] for item in log] == [timedelta(hours=12)]  # only the catalog's
        entity = hass.data["entity_components"]["update"].get_entity(ENTITY)
        assert entity._poll_seconds() is None
    finally:
        [c.stop() for c in ctx]


async def test_an_unknown_option_value_falls_back_to_the_default(hass, enable_bluetooth, fast_batch):
    entry, ctx, log = await setup_with_timers(hass, options={"device_poll_interval": "bogus"})
    try:
        entity = hass.data["entity_components"]["update"].get_entity(ENTITY)
        assert entity._poll_seconds() == 3 * 86400
    finally:
        [c.stop() for c in ctx]


async def test_update_offers_do_not_depend_on_the_bluetooth_poll(hass, enable_bluetooth, fast_batch):
    """Even with "never", the catalog timer refreshes the offer over HTTP and opens no connection."""
    entry, ctx, log = await setup_with_timers(hass, options={"device_poll_interval": "never"})
    try:
        entity = hass.data["entity_components"]["update"].get_entity(ENTITY)
        entity._catalog = None
        newer = Catalog(version=0x99, beta=None, images=("bin/ATC_v99.bin",) * 32)
        with (
            patch.object(FirmwareSource, "async_catalog", return_value=newer),
            patch.object(upd.PvvxUpdateEntity, "_async_read_device") as read,
        ):
            entity._catalog_poll_due(None)
            await hass.async_block_till_done()
        read.assert_not_called()  # no Bluetooth
        assert entity._catalog is newer
        # the device timer's callback does read (and writes the state)
        with patch.object(upd.PvvxUpdateEntity, "_async_read_device") as read:
            entity._device_poll_due(None)
            await hass.async_block_till_done()
        read.assert_awaited_once()
    finally:
        [c.stop() for c in ctx]


async def test_the_options_flow_offers_the_interval_with_three_days_as_default(hass, enable_bluetooth, fast_batch):
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        key = next(k for k in flow["data_schema"].schema if k == "device_poll_interval")
        assert key.default() == "3d"
        result = await hass.config_entries.options.async_configure(
            flow["flow_id"],
            {
                "min_battery_v": 2.5,
                "auto_clock_sync": True,
                "auto_restore_encryption": True,
                "device_poll_interval": "7d",
            },
        )
        assert result["data"]["device_poll_interval"] == "7d"
    finally:
        [c.stop() for c in ctx]


async def test_an_unrelated_entry_update_does_not_restart_the_poll_timer(hass, enable_bluetooth, fast_batch):
    from datetime import timedelta

    log, patcher = recording_timers()
    with patcher:
        entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
        try:
            before = len(log)
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, "installed_version": 0x58}
            )  # a stored version
            await hass.async_block_till_done()
            assert len(log) == before and not any(i["cancelled"] for i in log if i["interval"] == timedelta(days=3))
        finally:
            [c.stop() for c in ctx]


# -- signal strength before a flash: a warning and attributes, never a refusal -----------------------------------


async def test_weak_signal_warns_and_is_named_when_the_flash_fails(setup, caplog):
    hass, entry, entity = setup
    with patch.object(upd, "expected_path", return_value=("proxy-3", -80)):
        notice = entity._weak_signal_notice()
        assert "-80 dBm via proxy-3" in notice
        attrs = entity.extra_state_attributes
        assert attrs["expected_proxy"] == "proxy-3" and attrs["expected_proxy_rssi"] == -80
    with patch.object(upd, "expected_path", return_value=("proxy-2", -60)):
        assert entity._weak_signal_notice() is None  # strong enough: no warning
    with patch.object(upd, "expected_path", return_value=None):
        assert entity._weak_signal_notice() is None
        assert "expected_proxy" not in entity.extra_state_attributes


async def test_a_failed_flash_names_the_weak_signal_and_a_slow_link_is_a_normal_flash_failure(setup, caplog):
    hass, entry, entity = setup
    from custom_components.pvvx_ota.ota import OtaSlowLink

    caplog.set_level(logging.WARNING)
    with (
        patch.object(upd, "expected_path", return_value=("proxy-3", -80)),
        patch.object(upd, "flash", side_effect=OtaSlowLink("link too slow (2.0 blocks/s)")),
        patch.object(upd.PvvxUpdateEntity, "_check_battery"),
        patch.object(
            FirmwareSource,
            "async_catalog",
            return_value=Catalog(version=89, beta=None, images=("bin/ATC_v59.bin",) * 32),
        ),
        patch.object(FirmwareSource, "async_download", return_value=b"x"),
        pytest.raises(HomeAssistantError, match="link too slow .*weak signal: -80 dBm via proxy-3"),
    ):
        await entity.async_flash_latest()
    assert "weak signal: -80 dBm" in caplog.text  # warned before the transfer
    assert "failed" in str(entity._store_cfg.last_flash["result"])


# -- a lost bond is never an unhandled exception ------------------------------------------------------


def _refusing(*_a, **_k):
    from custom_components.pvvx_ota.ota import PairingRequiredError

    raise PairingRequiredError("the bond does not work any more")


async def test_poll_refresh_and_verify_turn_a_lost_bond_into_a_log_line_and_an_issue(setup, caplog):
    from homeassistant.helpers import issue_registry as ir

    from custom_components.pvvx_ota.const import DOMAIN

    hass, entry, entity = setup
    with patch.object(upd, "async_connected", _refusing), patch.object(store_mod, "async_connected", _refusing):
        await entity.async_update()  # poll: no exception
        assert "does not work any more" in caplog.text
        assert ir.async_get(hass).async_get_issue(DOMAIN, f"pin_set_{entry.entry_id}")
        with pytest.raises(HomeAssistantError, match="does not work"):
            await entry.runtime_data.async_refresh()  # a clean error for the button, not an unknown one
        # the verify loop gives up at once instead of retrying 8 times
        calls = []
        with patch.object(upd.asyncio, "sleep", side_effect=lambda s: calls.append(s)):
            await entity._async_verify_after_flash()
        assert calls == []


async def test_volatile_attributes_are_kept_out_of_the_recorder(setup):
    hass, entry, entity = setup
    excluded = entity._Entity__combined_unrecorded_attributes
    # what changes with every advertisement or operation is excluded, including what the update component excludes itself
    assert {"expected_proxy", "expected_proxy_rssi", "queued_position", "last_error", "in_progress"} <= excluded
    # and what describes the sensor stays recorded
    assert not {"installed_version", "latest_version", "hardware_id", "board", "beta_version"} & excluded
