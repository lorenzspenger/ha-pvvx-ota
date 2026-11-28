"""Repair issues that clear themselves: encryption changed (next BTHome reading) and settings reset (restore flow)."""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.pvvx_ota import config as cfg
from custom_components.pvvx_ota.const import DOMAIN

from .fake_gatt import FakeClient
from .test_config import KEY, SAMPLE, UPDATE, ent, fast_batch, issue, setup_cfg_entry  # noqa: F401
from .test_ha import bthome_device


def bthome_sensor(hass: HomeAssistant):
    """A BTHome temperature entity on the sensor's device, as HA's bthome integration would have created it."""
    bt, dev = bthome_device(hass)
    reg = er.async_get(hass).async_get_or_create("sensor", "bthome", "temp_uid", config_entry=bt, device_id=dev.id)
    return reg.entity_id


async def encrypt(hass):
    await hass.services.async_call(
        DOMAIN, "set_encryption", {"entity_id": UPDATE, "enable": True, "key": KEY}, blocking=True, return_response=True
    )


async def test_encryption_changed_clears_on_the_next_bthome_reading(hass: HomeAssistant, enable_bluetooth, fast_batch):
    sensor = bthome_sensor(hass)
    hass.states.async_set(sensor, "unavailable")
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        await encrypt(hass)
        assert issue(hass, entry, "encryption_changed") and not issue(hass, entry, "encryption_changed").is_fixable
        hass.states.async_set(sensor, "unavailable")  # a state without data
        hass.states.async_set(sensor, "unknown")
        await hass.async_block_till_done()
        assert issue(hass, entry, "encryption_changed")
        hass.states.async_remove(sensor)  # BTHome reloaded: the entity goes and comes back with its restored state
        hass.states.async_set(sensor, "20.0")
        await hass.async_block_till_done()
        assert issue(hass, entry, "encryption_changed")
        hass.states.async_set(sensor, "21.5")
        await hass.async_block_till_done()
        assert issue(hass, entry, "encryption_changed") is None
        assert entry.runtime_data._unwatch is None
    finally:
        [c.stop() for c in ctx]


async def test_encryption_changed_clears_on_a_reading_with_an_unchanged_value(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    sensor = bthome_sensor(hass)
    hass.states.async_set(sensor, "21.5")
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        await encrypt(hass)
        assert issue(hass, entry, "encryption_changed")
        hass.states.async_set(sensor, "21.5")  # same state: only state_reported fires
        await hass.async_block_till_done()
        assert issue(hass, entry, "encryption_changed") is None
    finally:
        [c.stop() for c in ctx]


async def test_encryption_changed_stays_without_a_bthome_entity(hass: HomeAssistant, enable_bluetooth, fast_batch):
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))  # no BTHome device
    try:
        await encrypt(hass)
        assert issue(hass, entry, "encryption_changed")
        bthome_device(hass)  # a device but no entity
        entry.runtime_data.async_watch_bthome()
        assert issue(hass, entry, "encryption_changed")
    finally:
        [c.stop() for c in ctx]


async def test_encryption_changed_is_watched_after_a_restart_and_unwatched_on_unload(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    sensor = bthome_sensor(hass)
    hass.states.async_set(sensor, "unavailable")
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        await encrypt(hass)
        assert await hass.config_entries.async_reload(entry.entry_id)  # the issue outlives the reload
        await hass.async_block_till_done()
        store = entry.runtime_data
        assert issue(hass, entry, "encryption_changed") and store._unwatch is not None
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert store._unwatch is None
        hass.states.async_set(sensor, "20")
        await hass.async_block_till_done()
        assert issue(hass, entry, "encryption_changed")  # nobody watching while unloaded
    finally:
        [c.stop() for c in ctx]


DEFAULTS = bytes([0x55, 0x59, 0x01, 0x00, 0x04, 0x06, 0x01, 0x03, 0xA9, 0x13, 0x37, 0x0A, 0x00])


async def reset(hass, client):
    client.default_cfg = DEFAULTS
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    button = ent(hass, "button", "reset_defaults")
    er.async_get(hass).async_update_entity(button, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    return entry, ctx


async def start_flow(hass, issue_id):
    assert await async_setup_component(hass, "repairs", {})
    mgr = hass.data["repairs"]["flow_manager"]
    return mgr, await mgr.async_init(DOMAIN, context={"issue_id": issue_id})


async def test_reset_defaults_repair_flow_restores_and_clears(hass: HomeAssistant, enable_bluetooth, fast_batch):
    entry, ctx = await reset(hass, FakeClient(cfg=SAMPLE))
    try:
        client_cfg = entry.runtime_data
        assert cfg.get(client_cfg.raw, "adv_interval") == 1
        mgr, flow = await start_flow(hass, f"reset_defaults_{entry.entry_id}")
        assert flow["type"] == "form" and flow["step_id"] == "confirm"
        result = await mgr.async_configure(flow["flow_id"], {})
        assert result["type"] == "create_entry"
        assert cfg.get(client_cfg.raw, "adv_interval") == 16
        assert issue(hass, entry, "reset_defaults") is None
    finally:
        [c.stop() for c in ctx]


async def test_reset_defaults_repair_flow_failure_keeps_the_issue(
    hass: HomeAssistant, enable_bluetooth, fast_batch, monkeypatch
):
    entry, ctx = await reset(hass, FakeClient(cfg=SAMPLE))
    try:
        store = entry.runtime_data

        async def fail():
            from homeassistant.exceptions import HomeAssistantError

            raise HomeAssistantError("boom")

        monkeypatch.setattr(store, "async_restore", fail)
        mgr, flow = await start_flow(hass, f"reset_defaults_{entry.entry_id}")
        result = await mgr.async_configure(flow["flow_id"], {})
        assert result["type"] == "form" and result["errors"] == {"base": "restore_failed"}
        assert issue(hass, entry, "reset_defaults")
    finally:
        [c.stop() for c in ctx]


async def test_reset_defaults_repair_flow_aborts_without_a_snapshot(hass: HomeAssistant, enable_bluetooth, fast_batch):
    entry, ctx = await reset(hass, FakeClient(cfg=SAMPLE))
    try:
        hass.config_entries.async_update_entry(entry, data={k: v for k, v in entry.data.items() if k != "cfg_snapshot"})
        mgr, flow = await start_flow(hass, f"reset_defaults_{entry.entry_id}")
        assert flow["type"] == "abort" and flow["reason"] == "cannot_restore"
        assert issue(hass, entry, "reset_defaults")
    finally:
        [c.stop() for c in ctx]


async def test_restore_button_clears_reset_defaults(hass: HomeAssistant, enable_bluetooth, fast_batch):
    entry, ctx = await reset(hass, FakeClient(cfg=SAMPLE))
    try:
        assert issue(hass, entry, "reset_defaults")
        await hass.services.async_call(
            "button", "press", {"entity_id": ent(hass, "button", "restore_config")}, blocking=True
        )
        assert issue(hass, entry, "reset_defaults") is None
    finally:
        [c.stop() for c in ctx]


async def test_both_issues_are_removed_with_the_entry(hass: HomeAssistant, enable_bluetooth, fast_batch):
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        for key in ("encryption_changed", "reset_defaults"):
            entry.runtime_data._issue(key)
        await hass.config_entries.async_remove(entry.entry_id)
        reg = ir.async_get(hass)
        assert not [i for i in reg.issues.values() if i[0] == DOMAIN]
    finally:
        [c.stop() for c in ctx]


def crypto_client(on: bool, key: bytes = bytes(range(1, 17))):
    client = FakeClient(cfg=SAMPLE)
    client.cfg = SAMPLE[:2] + cfg.apply(SAMPLE, {"adv_crypto": int(on)}, 10, frozenset({"adv_crypto"}))
    client.bindkey = key if on else bytes(16)
    return client


@pytest.mark.parametrize(
    "on,enable,key,raised",
    [
        (True, True, bytes(range(1, 17)).hex(), False),  # same key, flag already on
        (True, True, KEY, True),  # another key
        (True, False, None, True),  # switched off
        (False, False, None, False),  # already off
        (False, True, bytes(range(1, 17)).hex(), True),  # flag turns on
    ],
)
async def test_set_encryption_raises_the_issue_only_for_a_real_change(
    hass: HomeAssistant, enable_bluetooth, fast_batch, on, enable, key, raised
):
    client = crypto_client(on)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        data = {"entity_id": UPDATE, "enable": enable, **({"key": key} if key else {})}
        res = await hass.services.async_call(DOMAIN, "set_encryption", data, blocking=True, return_response=True)
        assert res[UPDATE]["encryption"] is enable
        assert bool(issue(hass, entry, "encryption_changed")) is raised
        assert cfg.get(client.cfg, "adv_crypto") == int(enable)
    finally:
        [c.stop() for c in ctx]


async def test_set_encryption_without_a_change_keeps_an_open_issue(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = crypto_client(False)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        entry.runtime_data._issue("encryption_changed")
        await hass.services.async_call(
            DOMAIN, "set_encryption", {"entity_id": UPDATE, "enable": False}, blocking=True, return_response=True
        )
        assert issue(hass, entry, "encryption_changed")
    finally:
        [c.stop() for c in ctx]


async def restore_after_reset(hass, client):
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    store = entry.runtime_data
    store.take_snapshot()
    client.cfg = client.cfg[:2] + cfg.apply(
        client.cfg, {"adv_interval": 1, "adv_crypto": 0}, 10, frozenset({"adv_crypto"})
    )
    await store.async_refresh()
    return entry, store, ctx


async def test_restore_switches_encryption_back_on_when_the_key_is_there(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = crypto_client(True)
    entry, store, ctx = await restore_after_reset(hass, client)
    try:
        assert cfg.get(client.cfg, "adv_crypto") == 0
        await store.async_restore()
        assert cfg.get(client.cfg, "adv_crypto") == 1 and cfg.get(client.cfg, "adv_interval") == 16
        assert issue(hass, entry, "encryption_restore_failed") is None
        assert issue(hass, entry, "encryption_changed") is None
    finally:
        [c.stop() for c in ctx]


async def test_restore_without_a_key_leaves_encryption_off_and_raises_the_issue(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = crypto_client(True)
    entry, store, ctx = await restore_after_reset(hass, client)
    try:
        client.bindkey = bytes(16)  # the key is gone
        await store.async_restore()
        assert cfg.get(client.cfg, "adv_crypto") == 0 and cfg.get(client.cfg, "adv_interval") == 16
        assert issue(hass, entry, "encryption_restore_failed")
        assert issue(hass, entry, "encryption_changed") is None
    finally:
        [c.stop() for c in ctx]


async def test_restore_leaves_encryption_alone_when_the_snapshot_had_it_off(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = crypto_client(False, key=bytes(range(1, 17)))
    entry, store, ctx = await restore_after_reset(hass, client)
    try:
        await store.async_restore()
        assert cfg.get(client.cfg, "adv_crypto") == 0 and cfg.get(client.cfg, "adv_interval") == 16
        assert issue(hass, entry, "encryption_restore_failed") is None
    finally:
        [c.stop() for c in ctx]


async def test_reset_defaults_stays_while_the_settings_still_differ(
    hass: HomeAssistant, enable_bluetooth, fast_batch, monkeypatch
):
    entry, ctx = await reset(hass, FakeClient(cfg=SAMPLE))
    try:
        store = entry.runtime_data

        async def nothing(changes):  # the sensor did not take the values
            return None

        monkeypatch.setattr(store, "async_request", nothing)
        await store.async_restore()
        assert issue(hass, entry, "reset_defaults")
        mgr, flow = await start_flow(hass, f"reset_defaults_{entry.entry_id}")
        result = await mgr.async_configure(flow["flow_id"], {})
        assert result["type"] == "form" and result["errors"] == {"base": "restore_failed"}
        assert issue(hass, entry, "reset_defaults")
    finally:
        [c.stop() for c in ctx]


async def test_a_read_with_the_saved_settings_clears_reset_defaults(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)
    entry, ctx = await reset(hass, client)
    try:
        assert issue(hass, entry, "reset_defaults")
        client.cfg = SAMPLE  # put back with another tool
        await entry.runtime_data.async_refresh()
        assert issue(hass, entry, "reset_defaults") is None
    finally:
        [c.stop() for c in ctx]


async def test_restore_is_offered_when_only_the_encryption_flag_was_lost(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = crypto_client(True)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        store = entry.runtime_data
        assert not store.can_restore
        store.take_snapshot()
        client.cfg = SAMPLE[:2] + cfg.apply(client.cfg, {"adv_crypto": 0}, 10, frozenset({"adv_crypto"}))
        store._issue("reset_defaults", fixable=True)
        await store.async_refresh()
        assert store.can_restore and issue(hass, entry, "settings_differ_after_flash") is None
        assert issue(hass, entry, "reset_defaults")  # a read with encryption still off keeps it
        await store.async_restore()
        assert issue(hass, entry, "reset_defaults") is None
        assert cfg.get(client.cfg, "adv_crypto") == 1 and not store.can_restore
    finally:
        [c.stop() for c in ctx]


async def test_encryption_changed_watches_a_bthome_entity_that_appears_later(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        await encrypt(hass)
        store = entry.runtime_data
        sensor = bthome_sensor(hass)  # the BTHome entry is added afterwards
        await hass.async_block_till_done()
        hass.states.async_set(sensor, "unavailable")
        hass.states.async_set(sensor, "21")
        await hass.async_block_till_done()
        assert issue(hass, entry, "encryption_changed") is None and store._unwatch is None
    finally:
        [c.stop() for c in ctx]


async def test_reset_at_defaults_raises_no_issue(hass: HomeAssistant, enable_bluetooth, fast_batch):
    entry, ctx = await reset(hass, FakeClient(cfg=DEFAULTS))
    try:
        assert issue(hass, entry, "reset_defaults") is None
    finally:
        [c.stop() for c in ctx]


async def test_old_notifications_are_dismissed_at_setup(hass: HomeAssistant, enable_bluetooth, fast_batch):
    from homeassistant.components import persistent_notification

    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        nid = f"{DOMAIN}_reset_defaults_{entry.entry_id}"
        persistent_notification.async_create(hass, "old", notification_id=nid)
        assert nid in hass.data["persistent_notification"]
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert nid not in hass.data["persistent_notification"]
    finally:
        [c.stop() for c in ctx]


async def reloaded_registry(hass: HomeAssistant, hass_storage) -> ir.IssueRegistry:
    """What Home Assistant has after a restart: a fresh registry loaded from the saved data."""
    hass_storage[ir.STORAGE_KEY] = {
        "version": ir.STORAGE_VERSION_MAJOR,
        "minor_version": ir.STORAGE_VERSION_MINOR,
        "key": ir.STORAGE_KEY,
        "data": ir.async_get(hass)._data_to_save(),
    }
    fresh = ir.IssueRegistry(hass)
    await fresh.async_load()
    return fresh


async def test_issues_are_active_again_after_a_restart(hass: HomeAssistant, enable_bluetooth, fast_batch, hass_storage):
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        entry.runtime_data._issue("encryption_changed")
        entry.runtime_data.flash_failed("boom")
        fresh = await reloaded_registry(hass, hass_storage)
        for issue_id in (f"encryption_changed_{entry.entry_id}", f"flash_failed_{entry.runtime_data.address}"):
            loaded = fresh.async_get_issue(DOMAIN, issue_id)
            assert loaded is not None and loaded.active
    finally:
        [c.stop() for c in ctx]


async def test_an_inactive_leftover_encryption_issue_is_not_watched(hass: HomeAssistant, enable_bluetooth, fast_batch):
    sensor = bthome_sensor(hass)
    hass.states.async_set(sensor, "unavailable")
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        entry.runtime_data._issue("encryption_changed")
        found = issue(hass, entry, "encryption_changed")
        assert found is not None
        object.__setattr__(found, "active", False)  # what a non-persistent issue of an older version loads as
        entry.runtime_data.async_watch_bthome()
        assert entry.runtime_data._unwatch is None
    finally:
        [c.stop() for c in ctx]


async def keep_key_call(hass, **extra):
    return await hass.services.async_call(
        DOMAIN,
        "set_encryption",
        {"entity_id": UPDATE, "enable": True, "keep_key": True, **extra},
        blocking=True,
        return_response=True,
    )


@pytest.mark.parametrize("flag_on,raised", [(False, True), (True, False)])
async def test_keep_key_switches_only_the_flag_on(
    hass: HomeAssistant, enable_bluetooth, fast_batch, caplog, flag_on, raised
):
    caplog.set_level("DEBUG")
    stored = bytes(range(1, 17))
    client = crypto_client(False, stored)
    client.cfg = SAMPLE[:2] + cfg.apply(SAMPLE, {"adv_crypto": int(flag_on)}, 10, frozenset({"adv_crypto"}))
    client.bindkey = stored
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        entry.runtime_data._issue("encryption_restore_failed")
        res = (await keep_key_call(hass))[UPDATE]
        assert res["encryption"] is True and "bindkey" not in res and "BTHome" in res["note"]
        assert cfg.get(client.cfg, "adv_crypto") == 1 and client.bindkey == stored
        assert stored.hex() not in caplog.text and stored.hex() not in str(res)
        assert bool(issue(hass, entry, "encryption_changed")) is raised
        assert not issue(hass, entry, "encryption_restore_failed")
    finally:
        [c.stop() for c in ctx]


async def test_keep_key_without_a_stored_key_changes_nothing(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = crypto_client(False)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        with pytest.raises(HomeAssistantError, match="no encryption key"):
            await keep_key_call(hass)
        assert cfg.get(client.cfg, "adv_crypto") == 0 and client.bindkey == bytes(16)
        assert not issue(hass, entry, "encryption_changed")
    finally:
        [c.stop() for c in ctx]


async def test_keep_key_together_with_a_key_is_refused(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = crypto_client(False)
    client.bindkey = bytes(range(1, 17))
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        with pytest.raises(ServiceValidationError, match="exclude each other"):
            await keep_key_call(hass, key=KEY)
        assert cfg.get(client.cfg, "adv_crypto") == 0 and client.bindkey == bytes(range(1, 17))
    finally:
        [c.stop() for c in ctx]


async def test_a_signal_strength_update_does_not_clear_encryption_changed(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    """BTHome updates the signal strength even when it cannot decrypt: only measurements count."""
    from homeassistant.const import EntityCategory

    bt, dev = bthome_device(hass)
    ent_reg = er.async_get(hass)
    rssi = ent_reg.async_get_or_create(
        "sensor",
        "bthome",
        "rssi_uid",
        config_entry=bt,
        device_id=dev.id,
        original_device_class="signal_strength",
        entity_category=EntityCategory.DIAGNOSTIC,
    ).entity_id
    diag = ent_reg.async_get_or_create(
        "sensor", "bthome", "diag_uid", config_entry=bt, device_id=dev.id, entity_category=EntityCategory.DIAGNOSTIC
    ).entity_id
    temp = ent_reg.async_get_or_create("sensor", "bthome", "temp_uid", config_entry=bt, device_id=dev.id).entity_id
    for eid in (rssi, diag, temp):
        hass.states.async_set(eid, "unavailable")
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        await encrypt(hass)
        for eid in (rssi, diag):
            hass.states.async_set(eid, "-70")
            hass.states.async_set(eid, "-65")
        await hass.async_block_till_done()
        assert issue(hass, entry, "encryption_changed")
        hass.states.async_set(temp, "21.5")
        await hass.async_block_till_done()
        assert issue(hass, entry, "encryption_changed") is None
    finally:
        [c.stop() for c in ctx]


@pytest.mark.parametrize("last", [True, False])
async def test_pairing_package_issue_goes_with_the_last_entry(hass: HomeAssistant, enable_bluetooth, fast_batch, last):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        if not last:
            MockConfigEntry(domain=DOMAIN, data={}, unique_id="other").add_to_hass(hass)
        ir.async_create_issue(
            hass,
            DOMAIN,
            "pairing_package_outdated_proxy",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="pairing_package_outdated",
            translation_placeholders={"proxy": "proxy"},
        )
        await hass.config_entries.async_remove(entry.entry_id)
        assert (ir.async_get(hass).async_get_issue(DOMAIN, "pairing_package_outdated_proxy") is None) is last
    finally:
        [c.stop() for c in ctx]
