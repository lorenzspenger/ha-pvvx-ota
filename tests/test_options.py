"""Options flow: battery threshold, automatic clock sync, automatic encryption restore."""

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError

from custom_components.pvvx_ota.const import (
    CONF_AUTO_CLOCK_SYNC,
    CONF_AUTO_RESTORE_ENCRYPTION,
    CONF_MIN_BATTERY_V,
)

from .fake_gatt import FakeClient
from .test_battery import HW0, add_voltage, flash
from .test_config import SAMPLE, fast_batch, setup_cfg_entry  # noqa: F401
from .translated import raises_key


async def test_options_form_defaults_save_and_keep_the_beta_switch(hass: HomeAssistant, enable_bluetooth):
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE), options={"beta": True})
    try:
        r = await hass.config_entries.options.async_init(entry.entry_id)
        assert r["type"] is FlowResultType.FORM and r["step_id"] == "init"
        defaults = {str(k): k.default() for k in r["data_schema"].schema}
        assert defaults == {
            CONF_MIN_BATTERY_V: 2.5,
            CONF_AUTO_CLOCK_SYNC: True,
            CONF_AUTO_RESTORE_ENCRYPTION: True,
            "device_poll_interval": "3d",
        }
        r = await hass.config_entries.options.async_configure(
            r["flow_id"],
            {
                CONF_MIN_BATTERY_V: 2.8,
                CONF_AUTO_CLOCK_SYNC: False,
                CONF_AUTO_RESTORE_ENCRYPTION: False,
                "device_poll_interval": "1d",
            },
        )
        assert r["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options == {
            "beta": True,  # the Beta firmware switch's value survives
            CONF_MIN_BATTERY_V: 2.8,
            CONF_AUTO_CLOCK_SYNC: False,
            CONF_AUTO_RESTORE_ENCRYPTION: False,
            "device_poll_interval": "1d",
        }
        # reopening shows the saved values
        r = await hass.config_entries.options.async_init(entry.entry_id)
        assert {str(k): k.default() for k in r["data_schema"].schema}[CONF_MIN_BATTERY_V] == 2.8
    finally:
        [c.stop() for c in ctx]


async def test_battery_threshold_option_is_used(hass: HomeAssistant, enable_bluetooth):
    add_voltage(hass, "3.0")
    client = FakeClient(cfg=HW0)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, options={CONF_MIN_BATTERY_V: 3.2})
    try:
        with raises_key("battery_low", ServiceValidationError, volts="3.00", limit="3.20"):
            await flash(hass)
        assert not client.writes
        hass.config_entries.async_update_entry(entry, options={CONF_MIN_BATTERY_V: 2.0})
        await flash(hass)  # now fine
        assert client.writes
    finally:
        [c.stop() for c in ctx]


async def test_automatic_encryption_restore_can_be_switched_off(hass: HomeAssistant, enable_bluetooth, fast_batch):
    """Same scenario as the restore test, but with the option off the flag stays off and nothing is written."""
    from unittest.mock import patch

    from . import test_config as tc

    real = tc.MockConfigEntry

    def with_option(*a, **k):
        k["options"] = {**k.get("options", {}), CONF_AUTO_RESTORE_ENCRYPTION: False}
        return real(*a, **k)

    with patch.object(tc, "MockConfigEntry", with_option):
        entry, state, clients = await tc.post_flash(hass, crypto_before=True, has_key=True)
    assert tc.cfg.get(state["cfg"], "adv_crypto") == 0
    assert sum(c.key_writes for c in clients) == 0 and all(c.cfg_writes == [] for c in clients)
    assert tc.issue(hass, entry, "encryption_restore_failed") is None  # not attempted, so not failed
