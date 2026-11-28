"""Battery check before a flash (voltage from the linked BTHome device's voltage sensor)."""

from datetime import timedelta
from unittest.mock import patch

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


def add_sensor(hass, uid, platform, state, device_class="voltage", unit="V"):
    if "_bt_test" not in hass.data:  # one BTHome device for all the sensors of a test
        hass.data["_bt_test"] = bthome_device(hass)
    bt, dev = hass.data["_bt_test"]
    reg = er.async_get(hass).async_get_or_create(
        "sensor", platform, uid, config_entry=bt, device_id=dev.id, original_device_class=device_class
    )
    hass.states.async_set(reg.entity_id, state, {"unit_of_measurement": unit, "device_class": device_class})
    return reg.entity_id


def later(hours):
    from homeassistant.util import dt as dt_util

    return patch(
        "custom_components.pvvx_ota.update.dt_util.utcnow", return_value=dt_util.utcnow() + timedelta(hours=hours)
    )


async def test_stale_voltage_is_ignored_and_the_age_logged(hass: HomeAssistant, enable_bluetooth, caplog):
    caplog.set_level("WARNING")
    add_voltage(hass, "2.0")  # would refuse, but it was reported 7 h ago
    client = FakeClient(cfg=HW0)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        entity = hass.data["entity_components"]["update"].get_entity("update.atc_000001")
        with later(7):
            assert entity._battery_voltage() is None
            entity._check_battery(False)  # no refusal: unknown voltage flashes anyway
        assert "battery voltage unknown" in caplog.text and "7.0 h ago" in caplog.text
    finally:
        [c.stop() for c in ctx]


async def test_fresh_voltage_is_used(hass: HomeAssistant, enable_bluetooth):
    add_voltage(hass, "2.0")
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=HW0), hw=0)
    try:
        entity = hass.data["entity_components"]["update"].get_entity("update.atc_000001")
        with later(5):
            assert entity._battery_voltage() == 2.0
    finally:
        [c.stop() for c in ctx]


async def test_bthome_voltage_wins_over_another_platform(hass: HomeAssistant, enable_bluetooth):
    add_sensor(hass, "other", "other_platform", "3.0")
    add_sensor(hass, "volt_uid", "bthome", "2.1")
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=HW0), hw=0)
    try:
        with raises_key("battery_low", ServiceValidationError, volts="2.10"):
            await flash(hass)
    finally:
        [c.stop() for c in ctx]


async def test_unavailable_bthome_voltage_falls_back_to_a_fresh_other_one(hass: HomeAssistant, enable_bluetooth):
    add_sensor(hass, "volt_uid", "bthome", "unavailable")
    add_sensor(hass, "other", "other_platform", "2.2")
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=HW0), hw=0)
    try:
        with raises_key("battery_low", ServiceValidationError, volts="2.20"):
            await flash(hass)
    finally:
        [c.stop() for c in ctx]


async def test_percentage_alone_is_not_converted(hass: HomeAssistant, enable_bluetooth, caplog):
    caplog.set_level("WARNING")
    add_sensor(hass, "pct", "bthome", "5", device_class="battery", unit="%")
    client = FakeClient(cfg=HW0)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        await flash(hass)
        assert client.writes and "only a battery percentage" in caplog.text
    finally:
        [c.stop() for c in ctx]
