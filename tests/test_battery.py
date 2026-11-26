"""Battery check before a flash (voltage from the linked BTHome device's voltage sensor)."""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.pvvx_ota import get_shared
from custom_components.pvvx_ota.const import DOMAIN

from .fake_gatt import FakeClient
from .test_config import SAMPLE, setup_cfg_entry
from .test_ha import bthome_device
from .translated import raises_key

HW0 = SAMPLE[:11] + bytes([0, 0])  # hardware id 0 is in the test catalog


def add_voltage(hass, state, unit="V"):
    bt, dev = bthome_device(hass)
    reg = er.async_get(hass).async_get_or_create(
        "sensor", "bthome", "volt_uid", config_entry=bt, device_id=dev.id, original_device_class="voltage"
    )
    if state is not None:
        hass.states.async_set(reg.entity_id, state, {"unit_of_measurement": unit, "device_class": "voltage"})
    return reg.entity_id


async def flash(hass, **data):
    await hass.services.async_call(DOMAIN, "flash_latest", {"entity_id": "update.atc_000001", **data}, blocking=True)


async def test_low_battery_refuses_the_flash(hass: HomeAssistant, enable_bluetooth, caplog):
    add_voltage(hass, "2.31")
    client = FakeClient(cfg=HW0)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        before = list(client.writes)
        with raises_key("battery_low", ServiceValidationError, volts="2.31", limit="2.50"):
            await flash(hass)
        assert client.writes == before  # nothing went over
        assert get_shared(hass).flashing is False
        assert hass.states.get("update.atc_000001").attributes["in_progress"] is False
    finally:
        [c.stop() for c in ctx]


async def test_force_overrides_a_low_battery(hass: HomeAssistant, enable_bluetooth, caplog):
    caplog.set_level("WARNING")
    add_voltage(hass, "2.31")
    client = FakeClient(cfg=HW0)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        await flash(hass, force=True)
        assert client.writes  # the image went over
        assert "flashing anyway (force)" in caplog.text
    finally:
        [c.stop() for c in ctx]


async def test_unknown_voltage_warns_and_continues(hass: HomeAssistant, enable_bluetooth, caplog):
    caplog.set_level("WARNING")
    client = FakeClient(cfg=HW0)  # no BTHome device at all: no voltage known
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        await flash(hass)
        assert client.writes and "battery voltage unknown" in caplog.text
    finally:
        [c.stop() for c in ctx]


@pytest.mark.parametrize("state,unit", [("3.0", "V"), ("2.5", "V"), ("2900", "mV")])
async def test_good_battery_passes(hass: HomeAssistant, enable_bluetooth, state, unit):
    add_voltage(hass, state, unit)
    client = FakeClient(cfg=HW0)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        await flash(hass)
        assert client.writes
    finally:
        [c.stop() for c in ctx]


async def test_millivolt_sensor_below_threshold_refuses(hass: HomeAssistant, enable_bluetooth):
    add_voltage(hass, "2400", "mV")
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=HW0), hw=0)
    try:
        with raises_key("battery_low", ServiceValidationError, volts="2.40"):
            await flash(hass)
    finally:
        [c.stop() for c in ctx]


async def test_unavailable_voltage_counts_as_unknown(hass: HomeAssistant, enable_bluetooth, caplog):
    caplog.set_level("WARNING")
    add_voltage(hass, "unavailable")
    client = FakeClient(cfg=HW0)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        await flash(hass)
        assert client.writes and "battery voltage unknown" in caplog.text
    finally:
        [c.stop() for c in ctx]
