"""The firmware queue: a second operation waits for the first instead of being refused ("Update all")."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pvvx_ota import get_shared
from custom_components.pvvx_ota.const import CONF_HW_ID, CONF_VERSION, DOMAIN
from custom_components.pvvx_ota.ota import OTA_CHAR

from .fake_gatt import FakeClient
from .test_ha import patch_catalog

MAC_A, MAC_B = "A4:C1:38:00:00:0A", "A4:C1:38:00:00:0B"
# 5.8 installed, hardware id 0 (in the test catalog): an update to 5.9 is available
BLOCK = bytes([0x55, 0x58, 0xA3, 0x05, 0x04, 0x06, 0x10, 0x04, 0xA9, 0x13, 0x37, 0x00, 0x00])


class Gated(FakeClient):
    """Logs which sensor sends OTA data; the first OTA write of the 'held' sensor waits for a release."""

    def __init__(self, name, log, gate=None):
        super().__init__(cfg=BLOCK)
        self.name, self.log, self.gate, self.first = name, log, gate, True

    async def write_gatt_char(self, char, data, response):
        if char == OTA_CHAR:
            if self.first and self.gate is not None:
                self.first = False
                self.log.append(f"{self.name}:started")
                await self.gate.wait()
            self.log.append(self.name)
        return await super().write_gatt_char(char, data, response)


async def setup_two(hass, gate_a):
    log: list[str] = []
    entries = {}
    for name, mac in (("A", MAC_A), ("B", MAC_B)):
        e = MockConfigEntry(
            domain=DOMAIN,
            unique_id=mac,
            title=f"SENSOR_{name}",
            data={CONF_ADDRESS: mac, CONF_VERSION: 0x58, CONF_HW_ID: 0},
        )
        e.add_to_hass(hass)
        entries[name] = e

    @asynccontextmanager
    async def fake(hass_, address, name):
        who = "A" if address == MAC_A else "B"
        c = Gated(who, log, gate_a if who == "A" else None)
        try:
            yield c
        finally:
            await c.disconnect()

    p1, p2 = patch_catalog()
    ctx = (
        p1,
        p2,
        patch("custom_components.pvvx_ota.update.async_connected", fake),
        patch("custom_components.pvvx_ota.store.async_connected", fake),
        patch("custom_components.pvvx_ota.update.async_call_later"),
    )
    for c in ctx:
        c.start()
    assert await hass.config_entries.async_setup(entries["A"].entry_id)  # sets up every entry of the domain
    await hass.async_block_till_done()
    return entries, log, ctx


async def test_two_parallel_installs_run_one_after_the_other(hass: HomeAssistant, enable_bluetooth):
    gate = asyncio.Event()
    entries, log, ctx = await setup_two(hass, gate)
    try:
        assert hass.states.get("update.sensor_a").state == "on" and hass.states.get("update.sensor_b").state == "on"
        a = hass.async_create_task(
            hass.services.async_call("update", "install", {"entity_id": "update.sensor_a"}, blocking=True)
        )
        for _ in range(100):
            await asyncio.sleep(0.02)
            if "A:started" in log:
                break
        b = hass.async_create_task(
            hass.services.async_call("update", "install", {"entity_id": "update.sensor_b"}, blocking=True)
        )
        await asyncio.sleep(0.2)
        st = hass.states.get("update.sensor_b")
        assert st.attributes["in_progress"] is True and st.attributes["update_percentage"] is None  # queued
        assert st.attributes["queued_position"] == 1
        assert "B" not in log  # nothing of B went over while A holds the lock
        gate.set()
        await asyncio.gather(a, b)
        await hass.async_block_till_done()
        order = [x for x in log if not x.endswith(":started")]
        assert order.count("A") > 0 and order.count("B") > 0
        first_b = order.index("B")
        assert all(x == "A" for x in order[:first_b]) and all(
            x == "B" for x in order[first_b:]
        )  # strictly one after the other
        assert hass.states.get("update.sensor_b").attributes["in_progress"] is False
        assert "queued_position" not in hass.states.get("update.sensor_b").attributes
        assert get_shared(hass).waiting == []
    finally:
        gate.set()
        [c.stop() for c in ctx]


async def test_queue_is_first_come_first_served_with_positions(hass: HomeAssistant, enable_bluetooth):
    gate = asyncio.Event()
    entries, log, ctx = await setup_two(hass, gate)
    try:
        a = hass.async_create_task(
            hass.services.async_call("update", "install", {"entity_id": "update.sensor_a"}, blocking=True)
        )
        for _ in range(100):
            await asyncio.sleep(0.02)
            if "A:started" in log:
                break
        b = hass.async_create_task(
            hass.services.async_call("update", "install", {"entity_id": "update.sensor_b"}, blocking=True)
        )
        await asyncio.sleep(0.1)
        assert [e._name for e in get_shared(hass).waiting] == ["SENSOR_B"]
        gate.set()
        await asyncio.gather(a, b)
    finally:
        gate.set()
        [c.stop() for c in ctx]


async def test_unload_while_queued_cancels_cleanly(hass: HomeAssistant, enable_bluetooth):
    gate = asyncio.Event()
    entries, log, ctx = await setup_two(hass, gate)
    try:
        a = hass.async_create_task(
            hass.services.async_call("update", "install", {"entity_id": "update.sensor_a"}, blocking=True)
        )
        for _ in range(100):
            await asyncio.sleep(0.02)
            if "A:started" in log:
                break
        b = hass.async_create_task(
            hass.services.async_call("update", "install", {"entity_id": "update.sensor_b"}, blocking=True)
        )
        await asyncio.sleep(0.1)
        assert len(get_shared(hass).waiting) == 1
        assert await hass.config_entries.async_unload(entries["B"].entry_id)  # B leaves while waiting
        await asyncio.gather(b, return_exceptions=True)
        assert b.cancelled() or b.exception() is not None
        assert get_shared(hass).waiting == []
        assert "B" not in log  # it never flashed
        gate.set()  # A is unaffected and finishes
        await a
        assert not get_shared(hass).lock.locked()
    finally:
        gate.set()
        [c.stop() for c in ctx]


async def test_queued_job_gives_up_after_the_cap(hass: HomeAssistant, enable_bluetooth):
    gate = asyncio.Event()
    entries, log, ctx = await setup_two(hass, gate)
    try:
        a = hass.async_create_task(
            hass.services.async_call("update", "install", {"entity_id": "update.sensor_a"}, blocking=True)
        )
        for _ in range(100):
            await asyncio.sleep(0.02)
            if "A:started" in log:
                break
        with patch("custom_components.pvvx_ota.update.QUEUE_TIMEOUT", 0.1):
            with pytest.raises(HomeAssistantError, match="waited more than"):
                await hass.services.async_call("update", "install", {"entity_id": "update.sensor_b"}, blocking=True)
        assert get_shared(hass).waiting == []
        assert hass.states.get("update.sensor_b").attributes["in_progress"] is False
        gate.set()
        await a
    finally:
        gate.set()
        [c.stop() for c in ctx]
