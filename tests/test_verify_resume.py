"""The post-flash version check survives a restart or reload of the entry (verify_pending marker in the entry data)."""

import asyncio
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.pvvx_ota import config as cfg
from custom_components.pvvx_ota.const import CONF_VERIFY_PENDING, FIRST_READ_STAGGER, VERIFY_PENDING_MAX_AGE

from .test_config import UPDATE, fast_batch, issue, setup_cfg_entry  # noqa: F401
from .test_eep_boundary import OLD_KEY, REPLACED, sensor
from .test_lifecycle import flash


def capture_timers():
    """Patch the timer of the post-flash check: keep (delay, callback) so the test starts the check itself."""
    started: list[tuple[float, object]] = []

    def fake(hass, delay, cb):
        if cb.__qualname__.endswith("_start"):  # the check only, not the catalog retry timers
            started.append((delay, cb))
        return lambda: None

    return started, patch("custom_components.pvvx_ota.flash_session.async_call_later", fake)


async def flash_then_restart(hass, entry):
    """Flash with the check pending, then unload and set the entry up again (what a restart does to it)."""
    started, timer = capture_timers()
    with timer:
        await flash(hass)
    assert len(started) == 1
    assert entry.data[CONF_VERIFY_PENDING]["at"]
    resumed, timer = capture_timers()
    with timer:
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    return started, resumed


async def run_resumed(hass, entry, resumed):
    assert len(resumed) == 1
    resumed[0][1](None)
    for _ in range(100):
        await asyncio.sleep(0.02)
        if CONF_VERIFY_PENDING not in entry.data:
            break
    await hass.async_block_till_done()


async def test_restart_after_a_same_side_flash_resumes_the_check_and_restores_encryption(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = sensor(version=0x55, flash_to=0x59)
    client.cfg = bytes([0x55, 0x58]) + client.cfg[2:]
    client.bindkey = OLD_KEY
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x58)
    try:
        started, resumed = await flash_then_restart(hass, entry)
        assert cfg.get(client.cfg, "adv_crypto") == 0  # the flash cleared the flag, nobody has checked yet
        assert CONF_VERIFY_PENDING in entry.data
        assert resumed[0][0] >= FIRST_READ_STAGGER
        await run_resumed(hass, entry, resumed)
        assert cfg.get(client.cfg, "adv_crypto") == 1 and client.bindkey == OLD_KEY
        assert hass.states.get(UPDATE).attributes["installed_version"] == "5.9"
        assert CONF_VERIFY_PENDING not in entry.data  # cleared after success
        assert issue(hass, entry, REPLACED) is None
    finally:
        [c.stop() for c in ctx]


async def test_restart_after_a_boundary_flash_raises_encryption_key_replaced(
    hass: HomeAssistant, enable_bluetooth, fast_batch, caplog
):
    client = sensor()
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x57)
    try:
        started, resumed = await flash_then_restart(hass, entry)
        assert "cannot carry the sensor coefficients over" in caplog.text
        await run_resumed(hass, entry, resumed)
        assert issue(hass, entry, REPLACED)
        assert cfg.get(client.cfg, "adv_crypto") == 0 and client.key_writes == 0  # no key to write back
        assert cfg.get(client.cfg, "adv_interval") == 16  # the saved settings came back
        assert "pending_key" not in entry.runtime_data.snapshot
        assert CONF_VERIFY_PENDING not in entry.data
    finally:
        [c.stop() for c in ctx]


async def test_a_stale_marker_is_ignored_and_cleared(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = sensor(version=0x55)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x58)
    try:
        stale = dt_util.utcnow().timestamp() - VERIFY_PENDING_MAX_AGE - 60
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_VERIFY_PENDING: {"expected": 0x59, "unverified": False, "at": stale}}
        )
        resumed, timer = capture_timers()
        with timer:
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
        assert resumed == [] and CONF_VERIFY_PENDING not in entry.data
    finally:
        [c.stop() for c in ctx]


async def test_a_fresh_marker_waits_out_the_rest_of_the_quiet_period(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = sensor(version=0x55)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x58)
    try:
        fresh = dt_util.utcnow().timestamp() - 5
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_VERIFY_PENDING: {"expected": 0x59, "unverified": False, "at": fresh}}
        )
        resumed, timer = capture_timers()
        with timer:
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
        assert len(resumed) == 1 and 20 <= resumed[0][0] <= 25
    finally:
        [c.stop() for c in ctx]


async def test_no_double_check_in_one_session_and_marker_survives_a_cancelled_check(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = sensor(version=0x55, flash_to=0x59)
    client.cfg = bytes([0x55, 0x58]) + client.cfg[2:]
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x58)
    try:
        started, timer = capture_timers()
        with timer:
            await flash(hass)
            await hass.async_block_till_done()
        assert len(started) == 1  # the in-session path schedules exactly one check, setup does not add one
        entity = hass.data["entity_components"]["update"].get_entity(UPDATE)
        with patch.object(type(entity), "_async_read_device", side_effect=asyncio.CancelledError):
            try:
                await entity._async_verify_after_flash()
            except asyncio.CancelledError:
                pass
        assert CONF_VERIFY_PENDING in entry.data  # a cancel (unload) leaves the marker for the next setup
        await entity._async_verify_after_flash()
        assert CONF_VERIFY_PENDING not in entry.data
    finally:
        [c.stop() for c in ctx]


async def test_a_failed_flash_leaves_no_marker(hass: HomeAssistant, enable_bluetooth, fast_batch):
    from .test_lifecycle import failing_flash

    client = sensor(version=0x55)
    client.fail_status_at = 3
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x58)
    try:
        await failing_flash(hass)
        assert CONF_VERIFY_PENDING not in entry.data
    finally:
        [c.stop() for c in ctx]
