"""Store error paths: refusals while flashing, failed verifications, encryption restore, reboot watch."""

import struct
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.pvvx_ota import calib as calibmod
from custom_components.pvvx_ota import get_shared
from custom_components.pvvx_ota import store as store_mod
from custom_components.pvvx_ota.const import CONF_SNAPSHOT

from .fake_gatt import FakeClient
from .test_config import SAMPLE, fast_batch, setup_cfg_entry  # noqa: F401


@pytest.fixture
async def setup(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    yield hass, client, entry, entry.runtime_data
    [c.stop() for c in ctx]


# -- reads -------------------------------------------------------------------------------------------


async def test_unreadable_coefficients_are_not_fatal(setup):
    hass, client, entry, store = setup
    with patch.object(calibmod, "parse", side_effect=calibmod.CalibError("odd reply")):
        await store.async_refresh()
    assert store.calib is None and store.raw is not None


async def test_first_read_waits_for_its_delay_and_a_failed_clock_set_is_reported(setup):
    hass, client, entry, store = setup
    await store.async_first_read(0.001)  # delay branch; the block is known already, so nothing is read
    client.device_time = 0
    real = client.write_gatt_char

    async def wrong_clock(char, data, response):
        if data[:1] == b"\x23" and len(data) >= 5:
            client.device_time = int.from_bytes(data[1:5], "little") + 1000  # the sensor keeps another time
            client.time_writes.append(bytes(data))
            client._cb(char, bytearray(b"\x23" + client.device_time.to_bytes(4, "little")))
            return
        return await real(char, data, response)

    client.write_gatt_char = wrong_clock
    with pytest.raises(HomeAssistantError, match="the sensor reports"):
        await store.async_sync_clock()


async def test_refresh_and_write_refuse_while_a_flash_runs(setup):
    hass, client, entry, store = setup
    shared = get_shared(hass)
    shared.flashing = True
    try:
        with pytest.raises(HomeAssistantError, match="flash is running"):
            await store.async_refresh()
        with pytest.raises(HomeAssistantError, match="settings were not changed"):
            await store._write({"adv_interval": 20})
        with pytest.raises(HomeAssistantError, match="nothing was changed"):
            async with store._session():
                pass
        assert not store.can_restore
    finally:
        shared.flashing = False


# -- restore and the encryption flag -------------------------------------------------------------------


async def test_restore_needs_a_snapshot_and_after_flash_without_one_does_nothing(setup):
    hass, client, entry, store = setup
    hass.config_entries.async_update_entry(entry, data={k: v for k, v in entry.data.items() if k != CONF_SNAPSHOT})
    with pytest.raises(HomeAssistantError, match="No saved settings"):
        await store.async_restore()
    await store.async_after_flash()  # no snapshot: nothing to do


async def test_encryption_restore_paths(setup):
    hass, client, entry, store = setup
    snap = {"crypto": True}
    store.raw = None
    await store._restore_encryption(snap)  # block unknown: left alone
    store.set_raw(SAMPLE)
    await store._restore_encryption({"crypto": False})  # it was off before: left alone
    # the flag is on already: a stale repair issue is cleared
    on = client.cfg[:7] + bytes([client.cfg[7] | 0x08]) + client.cfg[8:]
    from custom_components.pvvx_ota import config as cfgmod

    flg2 = cfgmod.FIELDS["adv_crypto"].index
    on = bytearray(SAMPLE)
    on[flg2] |= 0x08
    store.set_raw(bytes(on))
    store._issue("encryption_restore_failed")
    await store._restore_encryption(snap)
    from homeassistant.helpers import issue_registry as ir

    assert ir.async_get(hass).async_get_issue("pvvx_ota", f"encryption_restore_failed_{entry.entry_id}") is None
    # the sensor does not take the flag: reported as a warning and a repair issue
    store.set_raw(SAMPLE)
    client.cfg = SAMPLE
    client.clamp = lambda b: bytes(b[:flg2]) + bytes([b[flg2] & ~0x08]) + bytes(b[flg2 + 1 :])
    await store._restore_encryption(snap)
    assert ir.async_get(hass).async_get_issue("pvvx_ota", f"encryption_restore_failed_{entry.entry_id}")


async def test_set_encryption_refusals(setup):
    hass, client, entry, store = setup
    with pytest.raises(HomeAssistantError, match="32 hex characters"):
        await store.async_set_encryption(True, "nothex")
    # the sensor answers the key command with a different key
    with (
        patch.object(store_mod, "send_command", return_value=b"\x18" + bytes(16)),
        pytest.raises(HomeAssistantError, match="did not accept the key"),
    ):
        await store.async_set_encryption(True, "11" * 16)
    # the sensor stores the key but not the flag
    from custom_components.pvvx_ota import config as cfgmod

    flg2 = cfgmod.FIELDS["adv_crypto"].index
    client.clamp = lambda b: bytes(b[:flg2]) + bytes([b[flg2] & ~0x08]) + bytes(b[flg2 + 1 :])
    with pytest.raises(HomeAssistantError, match="did not switch encryption on.*run the action again"):
        await store.async_set_encryption(True, "22" * 16)
    on = bytearray(SAMPLE)
    on[flg2] |= 0x08
    client.cfg = bytes(on)
    client.clamp = lambda b: bytes(on)  # cannot be switched off
    with pytest.raises(HomeAssistantError, match="did not switch encryption off$"):
        await store.async_set_encryption(False)


async def test_pin_range_is_checked(setup):
    hass, client, entry, store = setup
    with pytest.raises(HomeAssistantError, match="up to 6 digits"):
        await store.async_set_pin(1_000_000)


# -- reboot watch -----------------------------------------------------------------------------------


async def test_reboot_watch_logs_the_return_or_the_silence(setup, caplog, monkeypatch):
    import logging

    caplog.set_level(logging.DEBUG)
    hass, client, entry, store = setup

    async def no_wait(_):
        return None

    monkeypatch.setattr(store_mod.asyncio, "sleep", no_wait)
    seen = SimpleNamespace(time=time.monotonic() + 1000)
    with patch("homeassistant.components.bluetooth.async_last_service_info", return_value=seen):
        await store._watch_return()
    assert "advertising again" in caplog.text
    monkeypatch.setattr(store_mod, "REBOOT_WATCH", 0.0)
    await store._watch_return()
    assert "not heard again" in caplog.text


# -- calibration verification -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "kwargs", "message"),
    [
        (lambda d: d[:10] + struct.pack("<h", 0), {"humi_offset": 2.0}, "stored a humidity offset"),
        (lambda d: struct.pack("<I", 1) + d[4:], {"temp_offset": 0.0}, "changed its slope factors"),
    ],
)
async def test_calibration_verification_failures(setup, mutate, kwargs, message):
    hass, client, entry, store = setup
    orig = client.write_gatt_char

    async def altered(char, data, response):
        if data[:1] == b"\x25" and len(data) > 1:
            data = data[:1] + mutate(data[1:])
        return await orig(char, data, response)

    client.write_gatt_char = altered
    with pytest.raises(HomeAssistantError, match=message):
        await store.async_set_calibration(**kwargs)


async def test_calibration_of_an_unknown_chip_is_refused(setup):
    hass, client, entry, store = setup
    client.coef = struct.pack("<IIhhIBB", 1, 1, 0, 0, 1, 0x70, 99)
    with pytest.raises(HomeAssistantError, match="not known"):
        await store.async_set_calibration(temp_offset=1.0)


async def test_change_during_an_inflight_write_goes_into_the_next_batch(setup):
    """A request that arrives while a batch is on the wire is written too, and its caller sees the real result."""
    import asyncio

    hass, client, entry, store = setup
    gate, entered = asyncio.Event(), asyncio.Event()
    orig = client.write_gatt_char

    async def slow(char, data, response):
        if data[:1] == b"\x55" and len(data) > 1 and not entered.is_set():
            entered.set()
            await gate.wait()  # the first batch hangs on the wire
        return await orig(char, data, response)

    client.write_gatt_char = slow
    first = asyncio.create_task(store.async_request({"screen_off": 1}))
    await entered.wait()
    second = asyncio.create_task(store.async_request({"smiley": 2}))
    await asyncio.sleep(0.2)
    assert not second.done()  # waits for the batch on the wire instead of returning
    gate.set()
    await asyncio.gather(first, second)
    await hass.async_block_till_done()
    assert len(client.cfg_writes) == 2
    assert store.value("screen_off") == 1 and store.value("smiley") == 2
    assert not store._pending
