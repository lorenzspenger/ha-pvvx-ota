"""Error paths of the device, firmware, diagnostics, select and button code that the feature tests do not reach."""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.pvvx_ota import device as dev
from custom_components.pvvx_ota import firmware as fw
from custom_components.pvvx_ota.const import DOMAIN
from custom_components.pvvx_ota.firmware import Catalog, FirmwareError, FirmwareSource

from .fake_gatt import FakeClient
from .test_config import MAC, SAMPLE, ent, fast_batch, setup_cfg_entry  # noqa: F401
from .translated import raises_key

# -- device ----------------------------------------------------------------------------------------


def test_unreachable_reason_uses_habluetooth_diagnostics_or_falls_back(hass: HomeAssistant):
    with patch.object(dev.bluetooth, "async_address_reachability_diagnostics", return_value="out of range"):
        assert dev._why_unreachable(hass, MAC) == "out of range"
    with patch.object(dev.bluetooth, "async_address_reachability_diagnostics", side_effect=RuntimeError("x")):
        assert "no connectable adapter or proxy" in dev._why_unreachable(hass, MAC)


async def test_connect_without_a_connectable_device_is_a_clear_error(hass: HomeAssistant):
    with (
        patch.object(dev.bluetooth, "async_ble_device_from_address", return_value=None),
        patch.object(dev, "_why_unreachable", return_value="never heard"),
        raises_key("not_connectable", reason="never heard"),
    ):
        async with dev.async_connected(hass, MAC, "ATC"):
            pass


async def test_connect_failure_and_the_device_refresh_callback(hass: HomeAssistant):
    first, later = object(), object()
    seen = []

    async def failing(client_cls, device, name, ble_device_callback=None):
        seen.append(ble_device_callback())  # the callback re-resolves the device for retries
        raise OSError("proxy busy")

    with (
        patch.object(dev.bluetooth, "async_ble_device_from_address", side_effect=[first, later]),
        patch.object(dev, "establish_connection", failing),
        raises_key("connect_failed", error="proxy busy"),
    ):
        async with dev.async_connected(hass, MAC, "ATC"):
            pass
    assert seen == [later]


async def test_disconnect_quietly_survives_errors_and_hangs(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)

    class Boom:
        async def disconnect(self):
            raise ConnectionError("already gone")

    await dev.disconnect_quietly(Boom(), "ATC")
    assert "disconnect ended with ConnectionError" in caplog.text

    class Hangs:
        async def disconnect(self):
            await asyncio.sleep(60)

    monkeypatch.setattr(dev, "DISCONNECT_TIMEOUT", 0.01)
    await dev.disconnect_quietly(Hangs(), "ATC")
    assert "did not confirm the disconnect" in caplog.text


# -- firmware ----------------------------------------------------------------------------------------


class Resp:
    def __init__(self, body=None, chunks=(), fail=None):
        self._body, self._chunks, self._fail = body, chunks, fail
        outer = self

        class Content:
            async def iter_chunked(self, n):
                for c in outer._chunks:
                    yield c

        self.content = Content()

    def raise_for_status(self):
        if self._fail:
            raise self._fail

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class Session:
    def __init__(self, resp):
        self.resp, self.calls = resp, 0

    def get(self, *a, **k):
        self.calls += 1
        return self.resp


CATALOG = {"version": 89, "custom": ["bin/ATC_v59.bin"]}


async def test_catalog_is_fetched_cached_and_failures_are_reported():
    import aiohttp

    session = Session(Resp(CATALOG))
    src = FirmwareSource(session)
    assert (await src.async_catalog()).version == 89
    assert (await src.async_catalog()).version == 89
    assert session.calls == 1  # cached
    # no catalog yet and the fetch fails: a clear error
    with pytest.raises(FirmwareError, match="cannot fetch firmware list"):
        await FirmwareSource(Session(Resp(fail=aiohttp.ClientError("offline")))).async_catalog()
    # an old catalog survives a failed refresh
    src._fetched = -1e9
    src._attempted = -1e9
    src._session = Session(Resp(fail=aiohttp.ClientError("offline")))
    assert (await src.async_catalog()).version == 89
    # nothing cached and nothing allowed to fetch (rate limit): "not available yet"
    young = FirmwareSource(Session(Resp(CATALOG)))
    young._attempted = 1e18
    with pytest.raises(FirmwareError, match="not available yet"):
        await young.async_catalog()


async def test_download_errors_are_firmware_errors():
    import aiohttp

    with pytest.raises(FirmwareError, match="refusing odd image path"):
        await FirmwareSource(Session(Resp())).async_download("../../etc/passwd")
    with pytest.raises(FirmwareError, match="download failed"):
        await FirmwareSource(Session(Resp(fail=aiohttp.ClientError("404")))).async_download("bin/ATC_v59.bin")
    big = Resp(chunks=[b"\x00" * 8192] * (fw.MAX_DOWNLOAD // 8192 + 2))
    with pytest.raises(FirmwareError, match="bigger than the BLE OTA limit"):
        await FirmwareSource(Session(big)).async_download("bin/ATC_v59.bin")


def test_board_for_refuses_an_image_without_a_board_name():
    cat = Catalog(version=89, beta=None, images=("bin/weird.bin",))
    with pytest.raises(FirmwareError, match="cannot derive board"):
        cat.board_for(0)


# -- diagnostics ---------------------------------------------------------------------------------


async def test_diagnostics_of_an_entry_that_never_read_the_sensor(hass: HomeAssistant, enable_bluetooth):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.pvvx_ota.diagnostics import _decode, async_get_config_entry_diagnostics

    assert _decode(None, None) is None
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, title="ATC", data={"address": MAC})
    entry.add_to_hass(hass)  # never set up: no runtime_data
    out = await async_get_config_entry_diagnostics(hass, entry)
    assert out["config_block"] is None or out["config_block"] == {}


async def test_diagnostics_skip_devices_that_vanished(hass: HomeAssistant, enable_bluetooth, fast_batch):
    from custom_components.pvvx_ota.diagnostics import async_get_config_entry_diagnostics

    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        with patch("homeassistant.helpers.device_registry.DeviceRegistry.async_get", return_value=None):
            out = await async_get_config_entry_diagnostics(hass, entry)
        assert out["devices"] == []
    finally:
        [c.stop() for c in ctx]


# -- select and button -------------------------------------------------------------------------------


@pytest.mark.real_defaults
async def test_tx_power_and_smiley_selects_write_and_ignore_raw_values(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        reg = er.async_get(hass)
        for dom, key in [("select", "rf_tx_power"), ("select", "smiley")]:
            reg.async_update_entity(ent(hass, dom, key), disabled_by=None)
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.first_read
        tx = ent(hass, "select", "rf_tx_power")
        from custom_components.pvvx_ota.select import TxPowerSelect

        await TxPowerSelect(hass, entry).async_select_option("raw 99")  # HA itself would reject the option
        assert client.cfg_writes == []  # "raw N" only reports the current value
        sm = ent(hass, "select", "smiley")
        option = hass.states.get(sm).attributes["options"][1]
        await hass.services.async_call("select", "select_option", {"entity_id": sm, "option": option}, blocking=True)
        assert len(client.cfg_writes) == 1
        label = hass.states.get(tx).attributes["options"][0]
        await hass.services.async_call("select", "select_option", {"entity_id": tx, "option": label}, blocking=True)
        assert len(client.cfg_writes) == 2
    finally:
        [c.stop() for c in ctx]


async def test_reflash_button_needs_the_update_entity(hass: HomeAssistant, enable_bluetooth, fast_batch):
    from custom_components.pvvx_ota.button import ReflashButton

    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        button = ReflashButton(hass, entry)
        entry.runtime_data.flash_latest = None
        with raises_key("update_entity_not_ready"):
            await button.async_press()
        flash = AsyncMock()
        entry.runtime_data.flash_latest = flash
        await button.async_press()
        flash.assert_awaited_once()
    finally:
        [c.stop() for c in ctx]


async def test_concurrent_first_calls_share_one_fetch():
    """Two update entities asking at startup: one download, both get the catalog (no 'not available yet')."""

    class Slow(Resp):
        async def json(self, content_type=None):
            await asyncio.sleep(0.05)
            return self._body

    session = Session(Slow(CATALOG))
    src = FirmwareSource(session)
    a, b = await asyncio.gather(src.async_catalog(), src.async_catalog())
    assert a.version == b.version == 89 and session.calls == 1


async def test_missing_catalog_is_retried_with_a_short_backoff(monkeypatch):
    import aiohttp

    from custom_components.pvvx_ota import firmware as fwmod

    now = [1000.0]
    monkeypatch.setattr(fwmod.time, "monotonic", lambda: now[0])
    src = FirmwareSource(Session(Resp(fail=aiohttp.ClientError("offline"))))
    with pytest.raises(FirmwareError, match="cannot fetch"):  # 1st attempt fails
        await src.async_catalog()
    now[0] += 30
    with pytest.raises(FirmwareError, match="not available yet"):  # still inside the 60 s backoff: no new fetch
        await src.async_catalog()
    assert src._session.calls == 1
    now[0] += 40  # 70 s after the failure
    with pytest.raises(FirmwareError, match="cannot fetch"):
        await src.async_catalog()
    assert src._session.calls == 2
    now[0] += 200  # the 2nd backoff is 5 minutes
    with pytest.raises(FirmwareError, match="not available yet"):
        await src.async_catalog()
    now[0] += 200
    src._session = Session(Resp(CATALOG))  # the network is back
    assert (await src.async_catalog()).version == 89
    assert src._failures == 0
