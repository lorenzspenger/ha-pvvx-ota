"""Beta firmware option."""

from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pvvx_ota import ota
from custom_components.pvvx_ota.const import CONF_HW_ID, CONF_VERSION, DOMAIN
from custom_components.pvvx_ota.firmware import FirmwareSource, choose_target, parse_catalog

from .fake_gatt import FakeClient
from .test_ha import MAC
from .test_ota import image

BASE = {
    "version": 89,
    "betaver": 96,
    "custom": ["bin/ATC_v59.bin", "bin/MHO_C401_v59.bin", "?", "bin/MHO_C401N_v59.bin"],
}


def cat(**extra):
    return parse_catalog({**BASE, **extra})


# -- target selection ---------------------------------------------------------------
def test_no_betafw_stays_stable_and_says_why():
    t = choose_target(cat(), 0, True)
    assert not t.is_beta and t.version == 0x59 and "no beta firmware for this hardware" in t.note
    assert choose_target(cat(), 0, False).note is None  # option off: nothing to explain


def test_no_betaver_published():
    data = {k: v for k, v in BASE.items() if k != "betaver"}
    t = choose_target(parse_catalog({**data, "betafw": ["bin/ATC_v60.bin"]}), 0, True)
    assert not t.is_beta and "no beta firmware is published" in t.note


def test_betafw_for_other_hardware_stays_stable():
    c = cat(betafw=["?", "bin/MHO_C401_v60.bin", "?", "/"])
    for hw in (0, 3, 2, None, 99):
        assert not choose_target(c, hw, True).is_beta
    assert choose_target(c, 1, True).is_beta


def test_beta_present_is_offered():
    t = choose_target(cat(betafw=["bin/ATC_v60.bin"]), 0, True)
    assert t.is_beta and t.version == 96 and t.source == "bin/ATC_v60.bin"
    assert not choose_target(cat(betafw=["bin/ATC_v60.bin"]), 0, False).is_beta  # option off


def test_beta_must_match_board_and_be_newer():
    wrong = choose_target(cat(betafw=["bin/MHO_C401_v60.bin"]), 0, True)  # hw 0 is an ATC
    assert not wrong.is_beta and "board" in wrong.note
    old = parse_catalog({**BASE, "version": 0x60, "betafw": ["bin/ATC_v60.bin"]})  # stable caught up with the beta
    assert not choose_target(old, 0, True).is_beta  # never a downgrade / sidegrade by itself
    nostable = choose_target(cat(betafw=["?", "?", "bin/CGG1_v60.bin"]), 2, True)  # hw 2: no stable image to compare
    assert not nostable.is_beta


@pytest.mark.parametrize(
    "src",
    [
        "https://evil.example/pvvx/ATC_v60.bin",
        "https://raw.githubusercontent.com.evil.com/pvvx/x_v60.bin",
        "https://raw.githubusercontent.com/other/repo/ATC_v60.bin",
        "http://raw.githubusercontent.com/pvvx/x/ATC_v60.bin",
        "https://github.com/pvvx/../evil/ATC_v60.bin",
        "bin/../ATC_v60.bin",
        "ATC_v60.bin",
    ],
)
def test_non_pvvx_sources_rejected(src):
    c = cat(betafw=[src])
    assert c.beta_source_for(0)[0] is None
    t = choose_target(c, 0, True)
    assert not t.is_beta and t.note


@pytest.mark.parametrize(
    "src",
    [
        "https://raw.githubusercontent.com/pvvx/ATC_MiThermometer/master/bin/ATC_v60.bin",
        "https://github.com/pvvx/ATC_MiThermometer/releases/download/v6.0/ATC_v60.bin",
    ],
)
def test_pvvx_urls_allowed(src):
    t = choose_target(cat(betafw=[src]), 0, True)
    assert t.is_beta and t.source == src


async def test_download_refuses_foreign_urls():
    source = FirmwareSource(session=None)
    for bad in ("https://evil.example/ATC_v60.bin", "http://github.com/pvvx/x_v60.bin", "bin/../x.bin"):
        with pytest.raises(ota.FirmwareError, match="odd image path"):
            await source.async_download(bad)


def test_betafw_not_a_list_is_ignored():
    assert cat(betafw="nonsense").beta_images == ()


# -- entity ---------------------------------------------------------------------------
async def setup_beta_entry(hass, catalog, options=None, hw=0):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC,
        title="ATC_000001",
        options=options or {},
        data={CONF_ADDRESS: MAC, CONF_VERSION: 0x59, CONF_HW_ID: hw},
    )
    entry.add_to_hass(hass)
    cfg_bytes = bytes([0x55, 0x59, 0xA3, 0x05, 0x04, 0x06, 0x10, 0x04, 0xA9, 0x13, 0x37, hw, 0x00])
    downloads: list[str] = []

    @asynccontextmanager
    async def fake(hass_, address, name):
        client = FakeClient(cfg=cfg_bytes)  # a fresh connection each time
        try:
            yield client
        finally:
            await client.disconnect()

    async def get_catalog(self):
        return catalog

    async def download(self, path):
        downloads.append(path)
        return image(500)

    ctx = (
        patch.object(FirmwareSource, "async_catalog", get_catalog),
        patch.object(FirmwareSource, "async_download", download),
        patch("custom_components.pvvx_ota.update.async_connected", fake),
        patch("custom_components.pvvx_ota.store.async_connected", fake),
        patch("custom_components.pvvx_ota.flash_session.async_call_later"),
    )
    for c in ctx:
        c.start()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, downloads, ctx


def beta_switch(hass):
    return er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{MAC}_beta_firmware")


async def test_default_off_stable(hass: HomeAssistant, enable_bluetooth):
    entry, downloads, ctx = await setup_beta_entry(hass, cat(betafw=["bin/ATC_v60.bin"]))
    try:
        reg = er.async_get(hass)
        assert reg.async_get(beta_switch(hass)).entity_category == "config"
        assert hass.states.get(beta_switch(hass)).state == "off"
        st = hass.states.get("update.atc_000001")
        assert st.attributes["latest_version"] == "5.9" and st.state == "off"
    finally:
        [c.stop() for c in ctx]


async def test_switch_on_offers_beta_and_flashes_the_beta_image(hass: HomeAssistant, enable_bluetooth):
    entry, downloads, ctx = await setup_beta_entry(hass, cat(betafw=["bin/ATC_v60.bin"]))
    try:
        await hass.services.async_call("switch", "turn_on", {"entity_id": beta_switch(hass)}, blocking=True)
        await hass.async_block_till_done()
        assert entry.options["beta"] is True  # stored in the entry options: survives restarts
        st = hass.states.get("update.atc_000001")
        assert st.attributes["latest_version"] == "6.0" and st.state == "on"
        assert "Beta firmware V6.0" in st.attributes["release_summary"]
        assert "newer than this integration knows" in st.attributes["release_summary"]
        await hass.services.async_call(DOMAIN, "flash_latest", {"entity_id": "update.atc_000001"}, blocking=True)
        assert downloads == ["bin/ATC_v60.bin"]
        # switching off returns to stable
        await hass.services.async_call("switch", "turn_off", {"entity_id": beta_switch(hass)}, blocking=True)
        await hass.async_block_till_done()
        assert hass.states.get("update.atc_000001").attributes["latest_version"] == "5.9"
        await hass.services.async_call(DOMAIN, "flash_latest", {"entity_id": "update.atc_000001"}, blocking=True)
        assert downloads[-1] == "bin/ATC_v59.bin"
    finally:
        [c.stop() for c in ctx]


async def test_option_survives_reload(hass: HomeAssistant, enable_bluetooth):
    entry, downloads, ctx = await setup_beta_entry(hass, cat(betafw=["bin/ATC_v60.bin"]), options={"beta": True})
    try:
        assert hass.states.get(beta_switch(hass)).state == "on"
        assert hass.states.get("update.atc_000001").attributes["latest_version"] == "6.0"
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get(beta_switch(hass)).state == "on"
    finally:
        [c.stop() for c in ctx]


async def test_on_but_no_beta_for_this_hardware(hass: HomeAssistant, enable_bluetooth):
    entry, downloads, ctx = await setup_beta_entry(
        hass, cat(betafw=["?", "bin/MHO_C401_v60.bin"]), options={"beta": True}
    )
    try:
        st = hass.states.get("update.atc_000001")
        assert st.attributes["latest_version"] == "5.9" and st.state == "off"
        assert "no beta firmware for this hardware" in st.attributes["beta_status"]
        assert "staying on stable" in st.attributes["release_summary"]
        await hass.services.async_call(DOMAIN, "flash_latest", {"entity_id": "update.atc_000001"}, blocking=True)
        assert downloads == ["bin/ATC_v59.bin"]
    finally:
        [c.stop() for c in ctx]


async def test_reflash_button_uses_the_beta_image_when_the_switch_is_on(hass: HomeAssistant, enable_bluetooth):
    entry, downloads, ctx = await setup_beta_entry(hass, cat(betafw=["bin/ATC_v60.bin"]), options={"beta": True})
    try:
        eid = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{MAC}_reflash_firmware")
        await hass.services.async_call("button", "press", {"entity_id": eid}, blocking=True)
        assert downloads == ["bin/ATC_v60.bin"]  # same decision as Install / flash_latest
    finally:
        [c.stop() for c in ctx]


def test_catalog_summary_shape_when_loaded():
    from custom_components.pvvx_ota.firmware import FirmwareSource

    src = FirmwareSource(session=None)
    assert src.summary()["loaded"] is False
    src._catalog = cat(betafw=["bin/ATC_v60.bin", "?"])
    s = src.summary()
    assert s["loaded"] and s["stable_version"] == "5.9" and s["beta_version"] == "6.0"
    assert s["images"] == 3 and s["beta_images"] == 1
