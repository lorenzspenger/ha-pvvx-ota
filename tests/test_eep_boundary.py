"""A flash across the 5.7/5.8 EEPROM rewrite erases the sensor's settings and bindkey: both are carried over."""

import json
import logging
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.pvvx_ota import config as cfg
from custom_components.pvvx_ota.const import DOMAIN
from custom_components.pvvx_ota.diagnostics import async_get_config_entry_diagnostics

from .fake_gatt import FakeClient
from .test_config import KEY, SAMPLE, UPDATE, fast_batch, issue, setup_cfg_entry  # noqa: F401
from .test_lifecycle import HW0, capture_verify, flash
from .translated import raises_key

OLD_KEY = bytes(range(1, 17))
REPLACED = "encryption_key_replaced"


def sensor(version=0x57, crypto=True, flash_to=0x59):
    """A sensor with settings that differ from the defaults it falls back to after an EEPROM erase."""
    client = FakeClient(cfg=bytes([0x55, version]) + HW0[2:])
    client.cfg = (
        client.cfg[:1]
        + bytes([version])
        + cfg.apply(client.cfg, {"adv_crypto": int(crypto)}, 0, frozenset({"adv_crypto"}))
    )
    client.bindkey = OLD_KEY if crypto else bytes(16)
    defaults = bytearray(HW0[2:])
    defaults[1] = 0  # flg2: smiley 0, encryption off
    defaults[4] = 50  # advertising interval
    client.eep_defaults = bytes(defaults)
    client.flash_to = flash_to
    return client


async def flash_and_verify(hass, entry):
    started, timer = capture_verify()
    with timer:
        await flash(hass)
    entity = hass.data["entity_components"]["update"].get_entity(UPDATE)
    return entity


async def test_boundary_flash_with_encryption_on_keeps_key_flag_and_settings(
    hass: HomeAssistant, enable_bluetooth, fast_batch, caplog
):
    caplog.set_level(logging.DEBUG)
    client = sensor()
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x57)
    try:
        entity = await flash_and_verify(hass, entry)
        assert client.cfg[1] == 0x59 and client.bindkey != OLD_KEY  # the sensor made up a new key
        assert client.cfg[2 + 4] == 50 and cfg.get(client.cfg, "adv_crypto") == 0  # and has defaults
        store = entry.runtime_data
        assert store._carry_key == OLD_KEY  # in memory only
        await entity._async_verify_after_flash()
        assert client.bindkey == OLD_KEY and client.key_writes == 1
        assert cfg.get(client.cfg, "adv_crypto") == 1
        assert cfg.get(client.cfg, "adv_interval") == cfg.get(HW0, "adv_interval") == 16
        assert cfg.get(client.cfg, "smiley") == cfg.get(HW0, "smiley")
        assert issue(hass, entry, REPLACED) is None and issue(hass, entry, "settings_differ_after_flash") is None
        assert store._carry_key is None and not store._carry_active
        assert "pending_key" not in store.snapshot
        assert "carried over" in caplog.text
        for secret in (OLD_KEY.hex(), str(list(OLD_KEY)), repr(OLD_KEY)):
            assert secret not in caplog.text
            assert secret not in json.dumps(await async_get_config_entry_diagnostics(hass, entry), default=str)
            assert secret not in json.dumps(dict(entry.data), default=str)
            assert secret not in repr(hass.states.get(UPDATE).attributes)
    finally:
        [c.stop() for c in ctx]


async def test_boundary_flash_sets_the_clock_after_the_key_and_the_settings(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    from homeassistant.util import dt as dt_util

    from custom_components.pvvx_ota import clock as clk

    client = sensor()
    client.restart_on_write = True  # every write restarts the sensor: its clock is stale again
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x57)
    try:
        entity = await flash_and_verify(hass, entry)
        await entity._async_verify_after_flash()
        assert client.commands[-4:] == ["key", "cfg", "cfg", "clock"]  # key, flag, settings; the clock is the last step
        assert abs(int.from_bytes(client.time_writes[-1][1:5], "little") - clk.local_epoch(dt_util.now())) <= 5
        assert client.bindkey == OLD_KEY and cfg.get(client.cfg, "adv_interval") == 16
    finally:
        [c.stop() for c in ctx]


async def test_boundary_flash_without_settings_changes_sets_the_clock_in_the_key_connection(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = sensor()
    client.restart_on_write = True
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x57)
    try:
        entity = await flash_and_verify(hass, entry)
        store = entry.runtime_data
        store.snapshot["fields"] = {}  # nothing but the key to carry over
        before = len(calls)
        await entity._async_verify_after_flash()
        assert client.commands[-3:] == ["key", "cfg", "clock"]  # key, flag, then the clock
        assert len(calls) - before <= 2  # the version read and the key connection: no extra one for the clock
    finally:
        [c.stop() for c in ctx]


async def test_boundary_flash_with_encryption_off_restores_settings_and_never_reads_the_key(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = sensor(crypto=False)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x57)
    try:
        entity = await flash_and_verify(hass, entry)
        await entity._async_verify_after_flash()
        assert client.key_reads == 0 and client.key_writes == 0
        assert cfg.get(client.cfg, "adv_crypto") == 0 and cfg.get(client.cfg, "adv_interval") == 16
        assert issue(hass, entry, REPLACED) is None and issue(hass, entry, "settings_differ_after_flash") is None
    finally:
        [c.stop() for c in ctx]


async def test_a_downgrade_across_the_boundary_is_carried_over_too(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = sensor(version=0x59, flash_to=0x57)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x59)
    try:
        with patch("custom_components.pvvx_ota.update.choose_target") as choose:
            from custom_components.pvvx_ota.firmware import Target

            choose.return_value = Target(0x57)
            entity = await flash_and_verify(hass, entry)
        await entity._async_verify_after_flash()
        assert client.bindkey == OLD_KEY and cfg.get(client.cfg, "adv_crypto") == 1
    finally:
        [c.stop() for c in ctx]


async def test_a_failed_write_back_leaves_encryption_off_and_raises_the_issue(
    hass: HomeAssistant, enable_bluetooth, fast_batch, caplog
):
    caplog.set_level(logging.DEBUG)
    client = sensor()
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x57)
    try:
        entity = await flash_and_verify(hass, entry)
        client.key_echo_wrong = True
        await entity._async_verify_after_flash()
        assert cfg.get(client.cfg, "adv_crypto") == 0
        found = issue(hass, entry, REPLACED)
        assert found and not found.is_fixable and found.translation_key == REPLACED
        assert cfg.get(client.cfg, "adv_interval") == 16  # the settings came back all the same
        assert entry.runtime_data._carry_key is None
        assert OLD_KEY.hex() not in caplog.text
    finally:
        [c.stop() for c in ctx]


async def test_a_key_that_could_not_be_read_raises_the_issue(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = sensor()
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x57)
    try:
        client.bindkey = bytes(16)  # the sensor answers "no key"
        entity = await flash_and_verify(hass, entry)
        await entity._async_verify_after_flash()
        assert issue(hass, entry, REPLACED) and cfg.get(client.cfg, "adv_crypto") == 0
    finally:
        [c.stop() for c in ctx]


async def test_a_restart_before_the_check_raises_the_issue_on_the_next_read(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = sensor()
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x57)
    try:
        await flash_and_verify(hass, entry)
        store = entry.runtime_data
        store.clear_carry()  # Home Assistant restarted: the key in memory is gone
        assert issue(hass, entry, REPLACED) is None
        await store.async_refresh()
        assert issue(hass, entry, REPLACED)
        assert "pending_key" not in store.snapshot
        # ... and the check itself, had it still run, would do the same
        store._clear_issue(REPLACED)
        entity = hass.data["entity_components"]["update"].get_entity(UPDATE)
        await entity._async_verify_after_flash()
        assert client.key_writes == 0
    finally:
        [c.stop() for c in ctx]


async def test_the_flag_only_restore_is_not_offered_while_the_issue_is_open(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = sensor()
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x57)
    try:
        entity = await flash_and_verify(hass, entry)
        client.key_echo_wrong = True
        await entity._async_verify_after_flash()
        assert issue(hass, entry, REPLACED)
        store = entry.runtime_data
        assert not store._crypto_lost()
        await store._restore_encryption(store.snapshot)
        assert cfg.get(client.cfg, "adv_crypto") == 0
    finally:
        [c.stop() for c in ctx]


async def test_a_flash_on_the_same_side_keeps_todays_behaviour(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = sensor(version=0x55, flash_to=0x59)  # the catalog image is 5.9: a crossing; use a same-side pair instead
    client.cfg = bytes([0x55, 0x58]) + client.cfg[2:]
    client.bindkey = OLD_KEY
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x58)
    try:
        entity = await flash_and_verify(hass, entry)
        assert client.bindkey == OLD_KEY  # the key survives a flash on the same side
        assert client.key_reads == 0 and entry.runtime_data._carry_key is None
        assert "pending_key" not in entry.runtime_data.snapshot
        await entity._async_verify_after_flash()
        assert client.key_writes == 0 and cfg.get(client.cfg, "adv_crypto") == 1  # flag only, key untouched
        assert issue(hass, entry, REPLACED) is None
    finally:
        [c.stop() for c in ctx]


async def test_a_failed_flash_forgets_the_key(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = sensor()
    client.fail_status_at = 3
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x57)
    try:
        from homeassistant.exceptions import HomeAssistantError

        with patch("custom_components.pvvx_ota.update.async_call_later"), pytest.raises(HomeAssistantError):
            await flash(hass)
        assert entry.runtime_data._carry_key is None and not entry.runtime_data._carry_active
    finally:
        [c.stop() for c in ctx]


async def test_keep_key_is_refused_while_the_issue_is_open_and_set_encryption_clears_it(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = FakeClient(cfg=SAMPLE)
    client.cfg = SAMPLE[:2] + cfg.apply(SAMPLE, {"adv_crypto": 0}, 10, frozenset({"adv_crypto"}))
    client.bindkey = bytes(range(9, 25))
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        entry.runtime_data._issue(REPLACED)
        with raises_key("keep_key_after_key_replaced", ServiceValidationError):
            await hass.services.async_call(
                DOMAIN,
                "set_encryption",
                {"entity_id": UPDATE, "enable": True, "keep_key": True},
                blocking=True,
                return_response=True,
            )
        assert cfg.get(client.cfg, "adv_crypto") == 0 and issue(hass, entry, REPLACED)
        await hass.services.async_call(
            DOMAIN,
            "set_encryption",
            {"entity_id": UPDATE, "enable": True, "key": KEY},
            blocking=True,
            return_response=True,
        )
        assert issue(hass, entry, REPLACED) is None and client.bindkey.hex() == KEY
    finally:
        [c.stop() for c in ctx]


async def test_a_generated_key_clears_the_issue_too(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        entry.runtime_data._issue(REPLACED)
        await hass.services.async_call(
            DOMAIN, "set_encryption", {"entity_id": UPDATE, "enable": True}, blocking=True, return_response=True
        )
        assert issue(hass, entry, REPLACED) is None
    finally:
        [c.stop() for c in ctx]


def test_the_boundary_helper():
    assert cfg.crosses_eep_boundary(0x57, 0x58) and cfg.crosses_eep_boundary(0x59, 0x55)
    assert not cfg.crosses_eep_boundary(0x55, 0x57) and not cfg.crosses_eep_boundary(0x58, 0x59)
    assert not cfg.crosses_eep_boundary(None, 0x59) and not cfg.crosses_eep_boundary(0x57, None)


async def test_flash_to_an_unverified_version_raises_the_issue_and_touches_nothing(
    hass: HomeAssistant, enable_bluetooth, fast_batch, caplog
):
    caplog.set_level(logging.INFO)
    from custom_components.pvvx_ota.firmware import Target

    client = sensor(version=0x59, flash_to=0x60)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x59)
    try:
        with patch("custom_components.pvvx_ota.update.choose_target") as choose:
            choose.return_value = Target(0x60)
            entity = await flash_and_verify(hass, entry)
        assert "has verified" in caplog.text
        await entity._async_verify_after_flash()
        found = issue(hass, entry, "firmware_unverified")
        assert found and not found.is_fixable and found.translation_placeholders["version"] == "6.0"
        assert client.key_reads == 0 and client.key_writes == 0
        assert cfg.get(client.cfg, "adv_crypto") == 0  # the flag was not switched back on
        store = entry.runtime_data
        assert store._carry_key is None and "pending_key" not in store.snapshot
        # a read on a verified version clears it (flashed back, or an integration update that verifies it)
        store.set_raw(bytes([0x55, 0x59]) + client.cfg[2:])
        assert issue(hass, entry, "firmware_unverified") is None
    finally:
        [c.stop() for c in ctx]


async def test_a_verified_flash_raises_no_unverified_issue(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = sensor()
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x57)
    try:
        entity = await flash_and_verify(hass, entry)
        await entity._async_verify_after_flash()
        assert issue(hass, entry, "firmware_unverified") is None
    finally:
        [c.stop() for c in ctx]
