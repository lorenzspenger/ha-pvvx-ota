"""Test fixtures."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let HA find custom_components/."""
    yield


@pytest.fixture(autouse=True)
def clean_serialx_registry():
    """Drop the URI handler HA's `usb` component registers, after every test.

    `usb` registers `esphome-hass-usb://` in serialx's process-global registry and only removes it from a stop-event
    callback. If a test's hass ends without that callback running, the next hass fails `usb` setup with "URI scheme is
    not unique", and with it `bluetooth` and this integration. Removing only the stub's entries keeps serialx's own.
    """
    yield
    try:
        from serialx.common import _REGISTERED_URI_HANDLERS
    except ImportError:  # serialx is only there with newer Home Assistant
        return
    for entries in _REGISTERED_URI_HANDLERS.values():
        entries[:] = [e for e in entries if e.handler.unique_scheme != "esphome-hass-usb://"]


@pytest.fixture(autouse=True)
def no_grace(monkeypatch):
    """Don't wait after the end marker in tests."""
    monkeypatch.setattr("custom_components.pvvx_ota.ota.END_MARKER_GRACE", 0)
    monkeypatch.setattr("custom_components.pvvx_ota.ota.DELAYED_REREQUEST_AFTER", 0)  # no 2 s wait in tests
    monkeypatch.setattr(
        "custom_components.pvvx_ota.ota.STALL_WINDOW", 0.05
    )  # a silent sensor with the link up is "unverified" fast


@pytest.fixture(autouse=True)
def no_history_fetch(monkeypatch):
    """Release notes come from pvvx's README over the network: none in tests unless a test patches it."""

    async def empty(self):
        return {}

    monkeypatch.setattr("custom_components.pvvx_ota.firmware.FirmwareSource.async_history", empty)


QUIET_BY_DEFAULT = {
    "adv_interval",
    "measure_interval",
    "rf_tx_power",
    "beta_firmware",
    "show_battery",
    "comfort_smiley",
    "screen_off",
    "temp_unit_display",
    "smiley",
    "refresh_config",
    "sync_clock",
    "reflash_firmware",
    "reboot_sensor",
}


@pytest.fixture(autouse=True)
def rarely_used_enabled(request, monkeypatch):
    """Rarely used settings ship disabled; most tests drive them, so enable them unless the test is marked `real_defaults`."""
    if request.node.get_closest_marker("real_defaults"):
        return
    from custom_components.pvvx_ota.entity import PvvxConfigEntity

    orig = PvvxConfigEntity.__init__

    def init(self, hass, entry, key, enabled_default=True):
        orig(self, hass, entry, key, True if key in QUIET_BY_DEFAULT else enabled_default)

    monkeypatch.setattr(PvvxConfigEntity, "__init__", init)


@pytest.fixture(autouse=True)
def sensor_was_heard(request, monkeypatch):
    """Entry setup needs a connectable scanner to have heard the sensor; tests marked `unheard` say nobody did."""
    heard = None if request.node.get_closest_marker("unheard") else object()
    monkeypatch.setattr("custom_components.pvvx_ota.async_ble_device_from_address", lambda *a, **k: heard)
