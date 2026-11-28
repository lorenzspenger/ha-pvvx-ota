"""What a freshly set up entry ships with, with nothing patched to be friendlier than the real thing.

The constants below restate docs/usage.md (entities enabled by default, the options table); a code change that flips a
default fails here and the documentation has to be changed with it.
"""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.pvvx_ota.const import (
    CONF_AUTO_CLOCK_SYNC,
    CONF_AUTO_RESTORE_ENCRYPTION,
    CONF_MIN_BATTERY_V,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    MIN_BATTERY_V,
    POLL_CHOICES,
)

from .fake_gatt import FakeClient
from .test_battery import add_voltage
from .test_config import SAMPLE, fast_batch, setup_cfg_entry  # noqa: F401
from .translated import raises_key

DOCUMENTED_ENABLED_BY_DEFAULT = {"firmware", "restore_config"}  # the update entity and Restore previous settings
DOCUMENTED_OPTION_DEFAULTS = {
    CONF_MIN_BATTERY_V: 2.5,
    CONF_AUTO_CLOCK_SYNC: True,
    CONF_AUTO_RESTORE_ENCRYPTION: True,
    CONF_POLL_INTERVAL: "3d",
}


@pytest.mark.real_defaults
async def test_a_fresh_entry_enables_exactly_the_documented_entities(hass: HomeAssistant, enable_bluetooth, fast_batch):
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        entries = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        assert len(entries) > 15  # the whole set was registered, not just the two
        enabled = {e.unique_id.split("_", 1)[1] for e in entries if e.disabled_by is None}
        assert enabled == DOCUMENTED_ENABLED_BY_DEFAULT
        for e in entries:
            if e.unique_id.split("_", 1)[1] not in DOCUMENTED_ENABLED_BY_DEFAULT:
                assert e.disabled_by is er.RegistryEntryDisabler.INTEGRATION, e.entity_id
        assert {s.entity_id for s in hass.states.async_all() if s.entity_id.split(".")[0] != "bluetooth"} >= {
            e.entity_id for e in entries if e.disabled_by is None
        }
    finally:
        [c.stop() for c in ctx]


@pytest.mark.real_defaults
async def test_a_fresh_entry_has_no_options_and_the_documented_option_defaults(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        assert dict(entry.options) == {}
        # the form shows the documented defaults ...
        r = await hass.config_entries.options.async_init(entry.entry_id)
        assert r["type"] is FlowResultType.FORM
        assert {str(k): k.default() for k in r["data_schema"].schema} == DOCUMENTED_OPTION_DEFAULTS
        assert (MIN_BATTERY_V, DEFAULT_POLL_INTERVAL) == (2.5, "3d") and POLL_CHOICES["3d"] == 3 * 86400
        # ... and the code that reads the options without a saved value uses the same ones
        assert entry.runtime_data.auto_clock_sync is True
        update = hass.data["entity_components"]["update"].get_entity("update.atc_000001")
        assert update._poll_seconds() == 3 * 86400 and update.beta_wanted is False
    finally:
        [c.stop() for c in ctx]


@pytest.mark.real_defaults
async def test_the_default_battery_limit_is_documented_2_5_volts(hass: HomeAssistant, enable_bluetooth, fast_batch):
    volts = add_voltage(hass, "2.6")
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        update = hass.data["entity_components"]["update"].get_entity("update.atc_000001")
        update._check_battery(False)  # above the default limit: fine
        hass.states.async_set(volts, "2.4", {"unit_of_measurement": "V", "device_class": "voltage"})
        with raises_key("battery_low", ServiceValidationError, volts="2.40", limit="2.50"):
            update._check_battery(False)
    finally:
        [c.stop() for c in ctx]
