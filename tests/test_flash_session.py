"""The flash session: unload or removal while a flash runs, and per-sensor flash state."""

import asyncio
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from custom_components.pvvx_ota import config as cfg
from custom_components.pvvx_ota import get_shared
from custom_components.pvvx_ota.const import CONF_VERIFY_PENDING, DOMAIN
from custom_components.pvvx_ota.flash_session import FlashSession

from .test_queue import BLOCK, MAC_A, MAC_B, setup_two
from .test_verify_resume import capture_timers, run_resumed
from .translated import raises_key


async def wait_for(cond, tries=200):
    for _ in range(tries):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


def start_install(hass, entity_id):
    return hass.async_create_task(
        hass.services.async_call("update", "install", {"entity_id": entity_id}, blocking=True)
    )


def hold_after_end_marker():
    """The transfer is through and the end marker is out; the watch for the reboot waits until ``release`` is set."""
    release = asyncio.Event()

    async def after_marker(client):
        await release.wait()

    return release, patch("custom_components.pvvx_ota.ota._after_marker", after_marker)


async def test_unload_during_the_transfer_cancels_it_and_keeps_the_old_firmware(hass: HomeAssistant, enable_bluetooth):
    gate = asyncio.Event()
    entries, log, ctx = await setup_two(hass, gate)
    try:
        entry = entries["A"]
        install = start_install(hass, "update.sensor_a")
        await wait_for(lambda: "A:started" in log)
        store = entry.runtime_data
        session = store.session
        assert session is not None and session.transferring and not session.end_marker_sent
        assert get_shared(hass).flashing_address == MAC_A and get_shared(hass).lock.locked()
        assert entry.data[CONF_VERIFY_PENDING]["expected"] == 0x59

        assert await hass.config_entries.async_unload(entry.entry_id)
        await asyncio.gather(install, return_exceptions=True)

        assert isinstance(install.exception(), HomeAssistantError)  # a flash error, not a crash
        assert "A" not in log  # nothing but the start was sent: no data, no end marker
        assert not get_shared(hass).lock.locked() and get_shared(hass).flashing_address is None
        assert store.session is None and session.stop_requested
        assert CONF_VERIFY_PENDING not in entry.data  # the old firmware is still on the sensor: nothing to check
        assert ir.async_get(hass).async_get_issue(DOMAIN, f"flash_failed_{MAC_A}") is None
        assert store.last_flash["result"].startswith("cancelled")
        # the sensor can be set up again and is not blocked
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data.session is None
    finally:
        gate.set()
        [c.stop() for c in ctx]


async def test_unload_after_the_end_marker_lets_the_transfer_end_and_resumes_the_check(
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

            assert await hass.config_entries.async_unload(entry.entry_id)
            assert not install.done()  # not cancelled: the sensor may be applying the image
            assert get_shared(hass).lock.locked() and not session.stop_requested and session.detached
            assert entry.data[CONF_VERIFY_PENDING]["expected"] == 0x59  # the marker survives the unload

            release.set()
            await install  # the flash ends without writing for the unloaded entity
        assert not get_shared(hass).lock.locked() and get_shared(hass).flashing_address is None
        assert (
            session._cancel_timer is None and session.verify_task is None
        )  # nothing was scheduled on the unloaded entry
        assert entry.data[CONF_VERIFY_PENDING]["expected"] == 0x59

        resumed, timer = capture_timers()  # the next setup resumes the check from the marker
        with timer:
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        assert len(resumed) == 1 and entry.runtime_data.session.expected == 0x59
        await run_resumed(hass, entry, resumed)
        assert CONF_VERIFY_PENDING not in entry.data and entry.runtime_data.session is None
    finally:
        release.set()
        [c.stop() for c in ctx]


async def test_removing_the_entry_during_the_transfer_cancels_it_and_writes_nothing(
    hass: HomeAssistant, enable_bluetooth
):
    gate = asyncio.Event()
    entries, log, ctx = await setup_two(hass, gate)
    try:
        entry = entries["A"]
        install = start_install(hass, "update.sensor_a")
        await wait_for(lambda: "A:started" in log)
        assert await hass.config_entries.async_remove(entry.entry_id) is not None
        await asyncio.gather(install, return_exceptions=True)
        assert isinstance(install.exception(), HomeAssistantError)
        assert hass.config_entries.async_get_entry(entry.entry_id) is None
        assert "A" not in log and not get_shared(hass).lock.locked() and get_shared(hass).flashing_address is None
        assert CONF_VERIFY_PENDING not in entry.data
        assert ir.async_get(hass).async_get_issue(DOMAIN, f"flash_failed_{MAC_A}") is None
    finally:
        gate.set()
        [c.stop() for c in ctx]


async def test_removing_the_entry_after_the_end_marker_writes_nothing_afterwards(hass: HomeAssistant, enable_bluetooth):
    entries, log, ctx = await setup_two(hass, None)
    release, patcher = hold_after_end_marker()
    try:
        entry = entries["A"]
        with patcher:
            install = start_install(hass, "update.sensor_a")
            await wait_for(
                lambda: entry.runtime_data.session is not None and entry.runtime_data.session.end_marker_sent
            )
            session = entry.runtime_data.session
            await hass.config_entries.async_remove(entry.entry_id)
            data_after_removal = dict(entry.data)
            release.set()
            await install
        assert hass.config_entries.async_get_entry(entry.entry_id) is None
        assert dict(entry.data) == data_after_removal  # the flash's tail did not touch the removed entry
        assert session._cancel_timer is None and session.verify_task is None
        assert not get_shared(hass).lock.locked() and get_shared(hass).flashing_address is None
        assert ir.async_get(hass).async_get_issue(DOMAIN, f"flash_failed_{MAC_A}") is None
    finally:
        release.set()
        [c.stop() for c in ctx]


async def test_flash_state_is_per_sensor_but_writes_are_blocked_for_all(hass: HomeAssistant, enable_bluetooth):
    gate = asyncio.Event()
    entries, log, ctx = await setup_two(hass, gate)
    try:
        store_a, store_b = entries["A"].runtime_data, entries["B"].runtime_data
        for store in (store_a, store_b):
            store.set_raw(BLOCK)
            store.take_snapshot()
            store.set_raw(BLOCK[:2] + cfg.apply(BLOCK, {"adv_interval": 20}, 0))  # differs from the snapshot
        assert store_a.available and store_b.available and store_a.can_restore and store_b.can_restore
        seen_b: list[tuple[bool, bool]] = []
        store_b.async_add_listener(lambda: seen_b.append((store_b.available, store_b.can_restore)))

        install = start_install(hass, "update.sensor_a")
        await wait_for(lambda: "A:started" in log)
        shared = get_shared(hass)
        assert shared.flashing and shared.flashing_address == MAC_A
        assert store_a.flashing and not store_b.flashing
        assert not store_a.available and not store_a.can_restore  # the sensor that is being flashed
        assert store_b.available and store_b.can_restore  # another sensor is not affected
        assert seen_b and seen_b[0] == (True, True)  # its entities were told when the flash started
        with raises_key("flash_running"):  # the rule stays: no configuration write while any flash runs
            await store_b.async_request({"screen_off": 1})

        n = len(seen_b)
        gate.set()
        await install
        assert shared.flashing_address is None and not shared.flashing
        assert len(seen_b) > n  # and when it ended
    finally:
        gate.set()
        [c.stop() for c in ctx]


async def test_set_flashing_notifies_every_store_once_per_change(hass: HomeAssistant, enable_bluetooth):
    entries, log, ctx = await setup_two(hass, None)
    try:
        calls = {"A": 0, "B": 0}
        for key in calls:
            entries[key].runtime_data.async_add_listener(lambda key=key: calls.__setitem__(key, calls[key] + 1))
        shared = get_shared(hass)
        shared.set_flashing(hass, MAC_B)
        shared.set_flashing(hass, MAC_B)  # no change, no notification
        assert calls == {"A": 1, "B": 1}
        shared.set_flashing(hass, None)
        assert calls == {"A": 2, "B": 2} and not shared.flashing
    finally:
        [c.stop() for c in ctx]


def test_the_session_never_shows_its_key():
    session = FlashSession.__new__(FlashSession)
    session.expected, session.unverified, session.end_marker_sent = 0x59, False, False
    session._carry_key = b"\x01" * 16
    assert "01" not in repr(session) and "carry" not in repr(session)
    assert session.take_carry_key() == b"\x01" * 16 and session.carry_key is None
