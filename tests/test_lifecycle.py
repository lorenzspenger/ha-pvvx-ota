"""Failed-flash repair issue, unload / reload / removal."""

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.pvvx_ota import update as upd
from custom_components.pvvx_ota.const import DOMAIN

from .fake_gatt import FakeClient
from .test_config import MAC, SAMPLE, ent, fast_batch, patch_catalog, setup_cfg_entry  # noqa: F401

HW0 = SAMPLE[:11] + bytes([0, 0])
ISSUE = f"flash_failed_{MAC}"


async def flash(hass):
    await hass.services.async_call(DOMAIN, "flash_latest", {"entity_id": "update.atc_000001"}, blocking=True)


async def failing_flash(hass):
    with patch("custom_components.pvvx_ota.update.async_call_later"), pytest.raises(HomeAssistantError):
        await flash(hass)


async def test_failed_flash_raises_one_issue_per_sensor_and_a_repeat_updates_it(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = FakeClient(cfg=HW0, fail_status_at=3, status_code=2)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        reg = ir.async_get(hass)
        assert reg.async_get_issue(DOMAIN, ISSUE) is None
        await failing_flash(hass)
        issue = reg.async_get_issue(DOMAIN, ISSUE)
        assert issue and issue.severity == ir.IssueSeverity.WARNING and not issue.is_fixable
        assert issue.translation_key == "flash_failed" and issue.translation_placeholders["error"]
        await failing_flash(hass)
        assert [i for i in reg.issues if i[0] == DOMAIN and i[1] == ISSUE] == [(DOMAIN, ISSUE)]  # still exactly one
    finally:
        [c.stop() for c in ctx]


async def test_link_loss_mid_flash_raises_the_issue_and_resets_the_entity(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = FakeClient(cfg=HW0, link_error_at=6)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        await failing_flash(hass)
        issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE)
        assert issue and "link failed" in issue.translation_placeholders["error"]
        st = hass.states.get("update.atc_000001")
        assert st.attributes["in_progress"] is False
        assert entry.runtime_data.last_flash["result"].startswith("failed")
    finally:
        [c.stop() for c in ctx]


async def test_next_successful_flash_clears_the_issue(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=HW0, fail_status_at=3, status_code=2)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        await failing_flash(hass)
        assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE)
        client.fail_status_at = None
        with patch("custom_components.pvvx_ota.update.async_call_later"):
            await flash(hass)
        assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE) is None
    finally:
        [c.stop() for c in ctx]


async def test_removing_the_entry_clears_its_issues(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=HW0, fail_status_at=3, status_code=2)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        await failing_flash(hass)
        store = entry.runtime_data
        store._issue("settings_differ_after_flash")
        reg = ir.async_get(hass)
        assert reg.async_get_issue(DOMAIN, ISSUE) and reg.async_get_issue(
            DOMAIN, f"settings_differ_after_flash_{entry.entry_id}"
        )
        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
        assert reg.async_get_issue(DOMAIN, ISSUE) is None
        assert reg.async_get_issue(DOMAIN, f"settings_differ_after_flash_{entry.entry_id}") is None
        assert not hasattr(entry, "runtime_data")
    finally:
        [c.stop() for c in ctx]


async def test_unload_and_reload(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        eid = ent(hass, "select", "temp_unit_display")
        assert hass.states.get(eid).state != "unavailable"
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert not hasattr(entry, "runtime_data")
        assert hass.states.get(eid).state == "unavailable"  # entities stay registered, but unavailable
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert hasattr(entry, "runtime_data")
        assert hass.states.get(eid).state != "unavailable"
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get(eid).state != "unavailable"
    finally:
        [c.stop() for c in ctx]


def test_link_description_has_no_duplicate_mac(hass: HomeAssistant):
    from types import SimpleNamespace

    from custom_components.pvvx_ota.device import describe_link

    with patch("custom_components.pvvx_ota.device.bluetooth.async_scanner_devices_by_address", return_value=[]):
        same = SimpleNamespace(_connected_scanner=SimpleNamespace(source="AA:BB", name="AA:BB"))
        assert describe_link(hass, same, MAC) == "AA:BB, RSSI unknown"
        named = SimpleNamespace(_connected_scanner=SimpleNamespace(source="AA:BB", name="proxy-1"))
        assert describe_link(hass, named, MAC) == "proxy-1 (AA:BB), RSSI unknown"


# -- config entry versions -----------------------------------------------------------------------
async def test_entry_saved_at_minor_version_2_still_loads(hass: HomeAssistant, enable_bluetooth, fast_batch):
    """Entries written by v0.11.0 have minor version 2; the handler is at 1.1 and needs no migration for that."""
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        assert entry.version == 1
        assert await hass.config_entries.async_unload(entry.entry_id)
        hass.config_entries.async_update_entry(entry, minor_version=2)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state.value == "loaded" and entry.minor_version == 2
        assert hasattr(entry, "runtime_data")
    finally:
        [c.stop() for c in ctx]


# -- non-blocking setup and the background first read ------------------------------------------------
async def test_setup_makes_no_connection_and_reads_in_the_background(
    hass: HomeAssistant, enable_bluetooth, fast_batch, request
):
    from unittest.mock import AsyncMock

    from homeassistant.config_entries import ConfigEntryState

    from custom_components.pvvx_ota import store as store_mod

    entry_data = {"address": MAC, "installed_version": 0x59, "hw_id": 10}
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, title="ATC_000001", data=entry_data)
    p1, p2 = patch_catalog()
    p1.start()
    p2.start()
    request.addfinalizer(p1.stop)
    request.addfinalizer(p2.stop)
    entry.add_to_hass(hass)
    gate = asyncio.Event()
    started = []

    async def slow_read(self):
        started.append(1)
        await gate.wait()
        self.set_raw(SAMPLE)

    with patch.object(store_mod.ConfigStore, "async_initial_read", slow_read):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED  # loaded while the read is still waiting
        eid = ent(hass, "number", "adv_interval")
        assert eid and hass.states.get(eid).state == "unavailable"  # entities exist, no data yet
        assert started == [1]
        gate.set()
        await entry.runtime_data.first_read
        await hass.async_block_till_done()
    assert hass.states.get(eid).state == "1.0"
    del AsyncMock


async def test_no_read_when_all_settings_entities_are_disabled(hass: HomeAssistant, enable_bluetooth, fast_batch):
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        reg = er.async_get(hass)
        for e in er.async_entries_for_config_entry(reg, entry.entry_id):
            if e.unique_id.split("_", 1)[1] not in ("firmware", "restore_config"):
                reg.async_update_entity(e.entity_id, disabled_by=er.RegistryEntryDisabler.USER)
        assert await hass.config_entries.async_unload(entry.entry_id)
        calls.clear()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        store = entry.runtime_data
        await store.first_read
        assert calls == [] and store.raw is None  # nothing connected
        assert not store.needs_read()
    finally:
        [c.stop() for c in ctx]


async def test_failed_first_read_retries_with_backoff(hass: HomeAssistant, enable_bluetooth, fast_batch, request):
    from homeassistant.exceptions import HomeAssistantError
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.pvvx_ota import store as store_mod

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=MAC, title="ATC_000001", data={"address": MAC, "installed_version": 0x59, "hw_id": 10}
    )
    p1, p2 = patch_catalog()
    p1.start()
    p2.start()
    request.addfinalizer(p1.stop)
    request.addfinalizer(p2.stop)
    entry.add_to_hass(hass)
    attempts = []

    async def flaky(self):
        attempts.append(1)
        if len(attempts) < 3:
            raise HomeAssistantError("not connectable")
        self.set_raw(SAMPLE)

    with (
        patch.object(store_mod.ConfigStore, "async_initial_read", flaky),
        patch("custom_components.pvvx_ota.store.FIRST_READ_RETRY", (0.01, 0.01, 0.01)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        eid = ent(hass, "number", "adv_interval")
        await entry.runtime_data.first_read
        await hass.async_block_till_done()
    assert len(attempts) == 3
    assert hass.states.get(eid).state == "1.0"


async def test_first_read_gives_up_after_the_last_retry(hass: HomeAssistant, enable_bluetooth, fast_batch, request):
    from homeassistant.exceptions import HomeAssistantError
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.pvvx_ota import store as store_mod

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=MAC, title="ATC_000001", data={"address": MAC, "installed_version": 0x59, "hw_id": 10}
    )
    p1, p2 = patch_catalog()
    p1.start()
    p2.start()
    request.addfinalizer(p1.stop)
    request.addfinalizer(p2.stop)
    entry.add_to_hass(hass)
    attempts = []

    async def never(self):
        attempts.append(1)
        raise HomeAssistantError("not connectable")

    with (
        patch.object(store_mod.ConfigStore, "async_initial_read", never),
        patch("custom_components.pvvx_ota.store.FIRST_READ_RETRY", (0.01, 0.01)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await entry.runtime_data.first_read
    assert len(attempts) == 3  # first try plus two retries, then the 12 h poll and the Refresh button take over
    assert hass.states.get(ent(hass, "number", "adv_interval")).state == "unavailable"


async def test_first_reads_are_staggered_while_ha_starts(hass: HomeAssistant, enable_bluetooth, fast_batch, request):
    """Not running yet: the n-th entry waits n * FIRST_READ_STAGGER after HA has started."""
    from homeassistant.core import CoreState
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.pvvx_ota import store as store_mod

    delays = []

    async def spy(self, delay=0.0):
        delays.append((self.address, delay))

    entries = [
        MockConfigEntry(
            domain=DOMAIN,
            unique_id=mac,
            title=mac,
            data={"address": mac, "installed_version": 0x59, "hw_id": 10},
        )
        for mac in ("A4:C1:38:00:00:01", "A4:C1:38:00:00:02", "A4:C1:38:00:00:03")
    ]
    p1, p2 = patch_catalog()
    p1.start()
    p2.start()
    request.addfinalizer(p1.stop)
    request.addfinalizer(p2.stop)
    for e in entries:
        e.add_to_hass(hass)
    hass.set_state(CoreState.starting)
    with patch.object(store_mod.ConfigStore, "async_first_read", spy):
        assert await hass.config_entries.async_setup(entries[0].entry_id)  # sets up all three
        await hass.async_block_till_done()
        assert delays == []  # nothing before HA has started
        hass.set_state(CoreState.running)
        await hass.async_start()
        await hass.async_block_till_done()
    assert sorted(delays) == [
        ("A4:C1:38:00:00:01", 0.0),
        ("A4:C1:38:00:00:02", 4.0),
        ("A4:C1:38:00:00:03", 8.0),
    ]


async def test_restore_works_from_the_snapshot_without_any_read(hass: HomeAssistant, enable_bluetooth, fast_batch):
    """No settings entity enabled, so no first read: "Restore previous settings" still has what it needs."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        store = entry.runtime_data
        store.take_snapshot()
        reg = er.async_get(hass)
        for e in er.async_entries_for_config_entry(reg, entry.entry_id):
            if e.unique_id.split("_", 1)[1] not in ("firmware", "restore_config"):
                reg.async_update_entity(e.entity_id, disabled_by=er.RegistryEntryDisabler.USER)
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        store = entry.runtime_data
        assert store.raw is None and store.can_restore  # snapshot only
        await hass.services.async_call(
            "button", "press", {"entity_id": ent(hass, "button", "restore_config")}, blocking=True
        )
        await hass.async_block_till_done()
    finally:
        [c.stop() for c in ctx]
    del MockConfigEntry


# -- the end marker was sent, then the link stalled ---------------------------------------------------------

UNCONFIRMED = "flash_unconfirmed_{}"


def capture_verify():
    """Patch the post-flash timer so the test can run the version check itself."""
    started = []

    def fake(hass, delay, cb):
        started.append(cb)
        return lambda: None

    return started, patch("custom_components.pvvx_ota.update.async_call_later", fake)


async def test_a_stall_after_the_end_marker_is_a_failed_unverified_flash_not_a_success(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = FakeClient(cfg=HW0, after_marker="stall")
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        started, timer = capture_verify()
        with timer, pytest.raises(HomeAssistantError, match="may or may not"):
            await flash(hass)
        assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE)  # not cleared, raised
        assert entry.runtime_data.last_flash["result"].startswith("unverified")
        assert hass.states.get("update.atc_000001").attributes["in_progress"] is False
        assert len(started) == 1  # the version is read back like after a normal flash
    finally:
        [c.stop() for c in ctx]


async def test_reading_the_new_version_after_an_unverified_flash_clears_the_issue(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = FakeClient(cfg=HW0, after_marker="stall")
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        started, timer = capture_verify()
        with timer, pytest.raises(HomeAssistantError):
            await flash(hass)
        reg = ir.async_get(hass)
        assert reg.async_get_issue(DOMAIN, ISSUE)
        entity = hass.data["entity_components"]["update"].get_entity("update.atc_000001")
        client.after_marker = "reboot"
        await entity._async_verify_after_flash(expected=0x59, unverified=True)  # the sensor answers with V5.9
        assert reg.async_get_issue(DOMAIN, ISSUE) is None
    finally:
        [c.stop() for c in ctx]


async def test_a_version_that_is_not_the_new_one_leaves_the_issue(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=HW0, after_marker="stall")
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        started, timer = capture_verify()
        with timer, pytest.raises(HomeAssistantError):
            await flash(hass)
        entity = hass.data["entity_components"]["update"].get_entity("update.atc_000001")
        await entity._async_verify_after_flash(expected=0x60, unverified=True)  # still the old version
        assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE)
    finally:
        [c.stop() for c in ctx]


async def test_a_version_check_that_never_works_raises_the_may_not_have_been_applied_issue(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=HW0), hw=0)
    try:
        reg = ir.async_get(hass)
        entity = hass.data["entity_components"]["update"].get_entity("update.atc_000001")
        sleeps = []

        async def no_wait(seconds):
            sleeps.append(seconds)

        with (
            patch("custom_components.pvvx_ota.update.asyncio.sleep", no_wait),
            patch.object(type(entity), "_async_read_device", side_effect=HomeAssistantError("out of range")),
        ):
            await entity._async_verify_after_flash()
        assert sleeps == [upd.POST_FLASH_RETRY] * upd.POST_FLASH_ATTEMPTS  # all attempts, spaced as configured
        issue = reg.async_get_issue(DOMAIN, UNCONFIRMED.format(entry.entry_id))
        assert issue and issue.translation_key == "flash_unconfirmed" and not issue.is_fixable
        # a later successful read clears it again
        await entity._async_verify_after_flash()
        assert reg.async_get_issue(DOMAIN, UNCONFIRMED.format(entry.entry_id)) is None
    finally:
        [c.stop() for c in ctx]


async def test_a_normal_flash_end_to_end_raises_no_issue_and_the_check_confirms(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    """Whole path through the update entity: connect, snapshot, flash, disconnect-type end, scheduled version check."""
    client = FakeClient(cfg=HW0, after_marker="reboot")
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        started, timer = capture_verify()
        with timer:
            await flash(hass)
        reg = ir.async_get(hass)
        assert not [i for i in reg.issues if i[0] == DOMAIN]  # no issue of any kind
        assert client.marker_sent and client.disconnected
        assert entry.runtime_data.last_flash["result"] == "ok"
        assert len(started) == 1
        entity = hass.data["entity_components"]["update"].get_entity("update.atc_000001")
        await entity._async_verify_after_flash()
        assert not [i for i in reg.issues if i[0] == DOMAIN]
        assert hass.states.get("update.atc_000001").attributes["in_progress"] is False
    finally:
        [c.stop() for c in ctx]


def test_a_sensor_gets_half_a_minute_to_reboot_and_the_check_retries_every_half_minute():
    """A pvvx sensor reboots in seconds: the quiet time, the first check and the retry spacing are 30 s, 8 attempts."""
    assert (upd.POST_FLASH_QUIET, upd.POST_FLASH_DELAY, upd.POST_FLASH_RETRY, upd.POST_FLASH_ATTEMPTS) == (
        30,
        30,
        30,
        8,
    )


async def test_flash_issues_clear_when_a_read_shows_the_latest_version(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "homeassistant", {})
    client = FakeClient(cfg=HW0, fail_status_at=3, status_code=2)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        reg = ir.async_get(hass)
        await failing_flash(hass)
        entry.runtime_data.flash_unconfirmed()
        unconfirmed = f"flash_unconfirmed_{entry.entry_id}"
        assert reg.async_get_issue(DOMAIN, ISSUE) and reg.async_get_issue(DOMAIN, unconfirmed)
        client.fail_status_at = None
        old, new = bytes([HW0[0], 0x50]) + HW0[2:], HW0
        client.cfg = old
        await hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": "update.atc_000001"}, blocking=True
        )
        assert reg.async_get_issue(DOMAIN, ISSUE) and reg.async_get_issue(DOMAIN, unconfirmed)  # still the old version
        client.cfg = new  # flashed with another tool
        await hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": "update.atc_000001"}, blocking=True
        )
        assert reg.async_get_issue(DOMAIN, ISSUE) is None and reg.async_get_issue(DOMAIN, unconfirmed) is None
        # the same through the refresh button (the store's read)
        client.cfg = old
        await hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": "update.atc_000001"}, blocking=True
        )
        entry.runtime_data.flash_failed("x")
        client.cfg = new
        await hass.services.async_call(
            "button", "press", {"entity_id": ent(hass, "button", "refresh_config")}, blocking=True
        )
        assert reg.async_get_issue(DOMAIN, ISSUE) is None
    finally:
        [c.stop() for c in ctx]
