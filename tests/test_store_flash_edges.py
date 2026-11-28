"""Failure and edge paths of the post-flash carry-over, the key/flag write-back and the cancellation of a running flash."""

import asyncio
import logging
import struct

import pytest
from homeassistant.core import HomeAssistant

from custom_components.pvvx_ota import config as cfg
from custom_components.pvvx_ota import get_shared
from custom_components.pvvx_ota.const import CONF_VERIFY_PENDING
from custom_components.pvvx_ota.errors import ha_error
from custom_components.pvvx_ota.ota import OtaError

from .test_config import fast_batch, issue, setup_cfg_entry  # noqa: F401
from .test_eep_boundary import CUSTOM_COEF, OLD_KEY, REPLACED, boundary_flash_with_coef, flash_and_verify, sensor
from .test_flash_session import hold_after_end_marker, start_install, wait_for
from .test_queue import setup_two
from .test_verify_resume import capture_timers, run_resumed

STORE = "custom_components.pvvx_ota.store"


def fail_coef_commands(monkeypatch, state):
    """The sensor-coefficient command (0x25) raises OtaError while ``state['on']``; everything else goes through."""
    from custom_components.pvvx_ota import store

    real = store.send_command

    async def send(client, payload, *a, **k):
        if state["on"] and payload[:1] == b"\x25":
            raise OtaError("no reply to the coefficient command")
        return await real(client, payload, *a, **k)

    monkeypatch.setattr(f"{STORE}.send_command", send)


# -- before the flash: the calibration read is best effort -------------------------------------------------


async def test_a_calibration_that_cannot_be_read_before_the_flash_does_not_stop_the_carry_over(
    hass: HomeAssistant, enable_bluetooth, fast_batch, monkeypatch, caplog
):
    state = {"on": True}
    fail_coef_commands(monkeypatch, state)
    client = sensor()
    entry, ctx, entity = await boundary_flash_with_coef(hass, client)
    try:
        assert entry.runtime_data.session.carry_calib is None
        assert "coefficients could not be read before the flash" in caplog.text
        assert entry.runtime_data.session.carry_key == OLD_KEY  # the key is still carried
        state["on"] = False
        await entity._async_verify_after_flash()
        assert client.bindkey == OLD_KEY and client.coef_writes == []
    finally:
        [c.stop() for c in ctx]


async def test_an_unknown_chip_type_with_a_zero_point_is_carried_over(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    unknown = struct.pack("<IIhhIBB", 17500, 10000, -3000, 100, 0x12345678, 0x70, 9)  # chip type 9: not in the table
    client = sensor()
    client.default_coef = struct.pack("<IIhhIBB", 17500, 10000, 0, 0, 0x12345678, 0x70, 9)
    entry, ctx, entity = await boundary_flash_with_coef(hass, client, coef=unknown)
    try:
        assert entry.runtime_data.session.carry_calib is not None
        await entity._async_verify_after_flash()
        assert client.coef == unknown
    finally:
        [c.stop() for c in ctx]


# -- after the flash --------------------------------------------------------------------------------------


async def test_a_different_chip_type_after_the_flash_keeps_the_new_firmwares_coefficients(
    hass: HomeAssistant, enable_bluetooth, fast_batch, caplog
):
    client = sensor()
    client.default_coef = CUSTOM_COEF[:-1] + bytes([2])  # the new firmware reports another chip type
    entry, ctx, entity = await boundary_flash_with_coef(hass, client)
    try:
        await entity._async_verify_after_flash()
        assert "chip type differs" in caplog.text
        assert client.coef_writes == [] and client.coef == client.default_coef
        assert client.bindkey == OLD_KEY  # key and settings are not affected
    finally:
        [c.stop() for c in ctx]


async def test_a_calibration_write_back_that_fails_is_a_warning_and_the_settings_still_come_back(
    hass: HomeAssistant, enable_bluetooth, fast_batch, monkeypatch, caplog
):
    client = sensor()
    entry, ctx, entity = await boundary_flash_with_coef(hass, client)
    try:
        state = {"on": True}  # reads before the flash worked; the write-back after it does not
        fail_coef_commands(monkeypatch, state)
        await entity._async_verify_after_flash()
        assert "calibration could not be written back" in caplog.text
        assert client.bindkey == OLD_KEY and cfg.get(client.cfg, "adv_interval") == 16
        assert entry.runtime_data.session is None or entry.runtime_data.session.carry_calib is None
    finally:
        [c.stop() for c in ctx]


async def test_settings_that_cannot_be_carried_over_are_a_warning_not_an_error(
    hass: HomeAssistant, enable_bluetooth, fast_batch, monkeypatch, caplog
):
    client = sensor()
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x57)
    try:
        entity = await flash_and_verify(hass, entry)
        store = entry.runtime_data

        async def refuse(changes):
            raise ha_error("flash_running")

        monkeypatch.setattr(store, "async_request", refuse)
        await entity._async_verify_after_flash()  # must not raise
        assert "settings could not be carried over the flash" in caplog.text
        assert client.bindkey == OLD_KEY and store._clock_last is False  # the key came back, the flag is reset
        assert store.session is None or not store.session.carry_active
    finally:
        [c.stop() for c in ctx]


@pytest.mark.parametrize("path", ["carry_over", "flag_only"])
async def test_a_sensor_that_does_not_report_encryption_on_after_the_write_raises_the_issue(
    hass: HomeAssistant, enable_bluetooth, fast_batch, monkeypatch, caplog, path
):
    caplog.set_level(logging.WARNING)
    client = sensor(version=0x58 if path == "flag_only" else 0x57)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x58 if path == "flag_only" else 0x57)
    try:
        entity = await flash_and_verify(hass, entry)

        async def echo_off(c, payload):
            return client.cfg  # the sensor answers with encryption still off

        monkeypatch.setattr(f"{STORE}.write_cfg", echo_off)
        if path == "flag_only":
            client.cfg = client.cfg[:2] + cfg.apply(client.cfg, {"adv_crypto": 0}, 0, frozenset({"adv_crypto"}))
            entry.runtime_data.set_raw(client.cfg)
        await entity._async_verify_after_flash()
        assert cfg.get(client.cfg, "adv_crypto") == 0
        assert issue(hass, entry, REPLACED if path == "carry_over" else "encryption_restore_failed")
        assert "could not write the encryption key back" in caplog.text or "could not switch encryption" in caplog.text
        assert OLD_KEY.hex() not in caplog.text
    finally:
        [c.stop() for c in ctx]


async def test_the_flag_restore_accepts_a_sensor_that_already_has_encryption_on(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    """The stored block says off (stale), the fresh read says on: nothing is written, the issue is cleared."""
    client = sensor(version=0x58)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x58)
    try:
        await flash_and_verify(hass, entry)
        store = entry.runtime_data
        on = client.cfg[:2] + cfg.apply(client.cfg, {"adv_crypto": 1}, 0, frozenset({"adv_crypto"}))
        off = client.cfg[:2] + cfg.apply(client.cfg, {"adv_crypto": 0}, 0, frozenset({"adv_crypto"}))
        client.cfg = on  # the sensor has encryption on ...
        store.set_raw(off)  # ... the stored block is older
        store._issue("encryption_restore_failed")
        writes = len(client.cfg_writes)
        await store._restore_encryption(store.snapshot)
        assert len(client.cfg_writes) == writes
        assert cfg.get(store.raw, "adv_crypto") == 1 and issue(hass, entry, "encryption_restore_failed") is None
    finally:
        [c.stop() for c in ctx]


# -- a restart inside the post-flash window ----------------------------------------------------------------


async def test_a_stranded_marker_is_ignored_when_the_flash_never_changed_the_side(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = sensor(version=0x58)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x58)
    try:
        await flash_and_verify(hass, entry)
        store = entry.runtime_data
        store.clear_carry()  # Home Assistant restarted ...
        store.snapshot["pending_key"] = True
        store._check_stranded_carry(bytes([0x55, 0x58]) + client.cfg[2:])  # ... and the sensor is still on 5.8
        assert issue(hass, entry, REPLACED) is None and store.snapshot.get("pending_key") is True
    finally:
        [c.stop() for c in ctx]


async def test_a_stranded_marker_with_encryption_still_on_raises_no_issue_and_is_dropped(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = sensor()
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x57)
    try:
        await flash_and_verify(hass, entry)
        store = entry.runtime_data
        store.clear_carry()
        store.snapshot["pending_key"] = True
        on = client.cfg[:2] + cfg.apply(client.cfg, {"adv_crypto": 1}, 0, frozenset({"adv_crypto"}))
        store._check_stranded_carry(on)  # crossed, but encryption is on: nothing was lost
        assert issue(hass, entry, REPLACED) is None and "pending_key" not in store.snapshot
    finally:
        [c.stop() for c in ctx]


# -- cancellation after the end marker (Home Assistant shutting down) ---------------------------------------


async def test_cancelling_the_flash_after_the_end_marker_keeps_the_marker_unverified_and_resumes(
    hass: HomeAssistant, enable_bluetooth
):
    entries, log, ctx = await setup_two(hass, None)
    release, patcher = hold_after_end_marker()
    try:
        entry = entries["A"]
        with patcher:
            install = start_install(hass, "update.sensor_a")
            await wait_for(
                lambda: entry.runtime_data.session is not None and entry.runtime_data.session.end_marker_sent
            )
            store = entry.runtime_data
            session = store.session
            install.cancel()  # not the unload path (stop_requested stays False): e.g. shutdown cancelling tasks
            await asyncio.gather(install, return_exceptions=True)
        assert install.cancelled() and not session.stop_requested
        assert not get_shared(hass).lock.locked() and get_shared(hass).flashing_address is None
        marker = entry.data[CONF_VERIFY_PENDING]
        assert marker["expected"] == 0x59 and marker["unverified"] is True  # the image may have been applied
        assert store.last_flash["result"] != "ok"

        resumed, timer = capture_timers()  # the next setup checks the version
        with timer:
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
        await run_resumed(hass, entry, resumed)
        assert CONF_VERIFY_PENDING not in entry.data
    finally:
        release.set()
        [c.stop() for c in ctx]
