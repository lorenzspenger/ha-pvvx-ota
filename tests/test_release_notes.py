"""Release notes from pvvx's README version history."""

from unittest.mock import patch

from homeassistant.components.update import DATA_COMPONENT, UpdateEntityFeature
from homeassistant.core import HomeAssistant

from custom_components.pvvx_ota import firmware
from custom_components.pvvx_ota.firmware import FirmwareSource, notes_between, parse_history, plain

from .fake_gatt import FakeClient
from .test_config import SAMPLE, setup_cfg_entry

README = """
# ATC_MiThermometer

| Version | Changes |
| ------: | ------- |
|     9.9 | not in the history section |

### Firmware version history

| Version | Changes |
| ------: | ------- |
|     5.5 | Added [ZBEACON-MC](https://pvvx.github.io/Zbeacon-MC). LCD SPI speed reduced. |
|     5.7 | Fix SHTC3 sensor... |
|     5.8 | Added ZG-204ZV. The procedure for saving settings in Flash has been changed. A reconfiguration will be required. |
|     5.9 | Fixed an error during the initial **OTA** download at address 0x40000. Added `ZBEACON-TH01` v2.0 |
|     --- |  |
|     6.0 | [Starting with version 6.0](https://github.com/pvvx/ATC_MiThermometer/issues/727), only BTHome v2 |

## Applications

|     1.0 | not part of the history either |
"""


def test_parse_history_only_the_version_table():
    h = parse_history(README)
    assert sorted(h) == [0x55, 0x57, 0x58, 0x59, 0x60]
    assert "reconfiguration will be required" in h[0x58]
    assert 0x99 not in h and 0x10 not in h  # other tables are ignored


def test_plain_strips_markdown_and_shortens():
    assert plain("Added [ZBEACON-MC](https://x/y). `code` **bold**") == "Added ZBEACON-MC. code bold"
    assert len(plain("x" * 400)) == 255 and plain("x" * 400).endswith("…")


def test_notes_between_versions_newest_first():
    h = parse_history(README)
    text = notes_between(h, 0x57, 0x59)
    assert text.index("V5.9") < text.index("V5.8") and "V5.7" not in text  # exclusive of the installed version
    assert notes_between(h, None, 0x59).startswith("**V5.9**") and "V5.8" not in notes_between(h, None, 0x59)
    assert notes_between(h, 0x59, 0x59) is None  # nothing newer
    assert notes_between({}, 0x57, 0x59) is None


class FakeResp:
    def __init__(self, text):
        self._t = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    async def text(self):
        return self._t


class FakeSession:
    def __init__(self, text=README, fail=False):
        self.calls, self.text, self.fail = 0, text, fail

    def get(self, url, **kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError("no network")
        return FakeResp(self.text)


async def test_history_is_cached_and_failures_are_non_fatal(monkeypatch):
    monkeypatch.undo()  # undo the autouse stub: the real method
    s = FakeSession()
    src = FirmwareSource(s)
    assert 0x59 in await src.async_history()
    assert 0x59 in await src.async_history()
    assert s.calls == 1  # cached with the catalog TTL
    assert await FirmwareSource(FakeSession(fail=True)).async_history() == {}  # never raises
    assert await FirmwareSource(FakeSession(text="no table here")).async_history() == {}


async def test_entity_shows_summary_url_and_notes(hass: HomeAssistant, enable_bluetooth):
    old = bytes([0x55, 0x57]) + SAMPLE[2:11] + bytes([0, 0])  # 5.7 installed, hardware id 0
    history = parse_history(README)

    with patch.object(FirmwareSource, "history", property(lambda self: history)):
        entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=old), hw=0, version=0x57)
        try:
            await hass.async_block_till_done()
            st = hass.states.get("update.atc_000001")
            assert st.attributes["release_url"] == firmware.HISTORY_PAGE
            assert st.attributes["latest_version"] == "5.9"
            assert st.attributes["release_summary"].startswith("Fixed an error during the initial OTA download")
            assert "**" not in st.attributes["release_summary"]
            assert st.attributes["supported_features"] & UpdateEntityFeature.RELEASE_NOTES
            entity = hass.data[DATA_COMPONENT].get_entity("update.atc_000001")
            notes = await entity.async_release_notes()
            assert "V5.9" in notes and "V5.8" in notes and "V5.7" not in notes and "reconfiguration" in notes
            assert "Source:" in notes
        finally:
            [c.stop() for c in ctx]


async def test_notes_fallback_when_the_readme_is_not_available(hass: HomeAssistant, enable_bluetooth):
    old = bytes([0x55, 0x57]) + SAMPLE[2:11] + bytes([0, 0])
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=old), hw=0, version=0x57)  # history stubbed empty
    try:
        entity = hass.data[DATA_COMPONENT].get_entity("update.atc_000001")
        assert "No release notes were found" in await entity.async_release_notes()
        assert hass.states.get("update.atc_000001").attributes["release_summary"] is None
    finally:
        [c.stop() for c in ctx]


async def test_concurrent_history_calls_share_one_fetch(monkeypatch):
    import asyncio

    monkeypatch.undo()  # the real method

    class Slow(FakeSession):
        def get(self, *a, **k):
            self.calls += 1
            return SlowResp(self.text)

    class SlowResp(FakeResp):
        async def text(self):
            await asyncio.sleep(0.05)
            return self._t

    s = Slow()
    src = FirmwareSource(s)
    a, b = await asyncio.gather(src.async_history(), src.async_history())
    assert 0x59 in a and a == b and s.calls == 1
    assert src.history == a


async def test_missing_history_is_retried_with_a_short_backoff(monkeypatch):
    monkeypatch.undo()
    now = [1000.0]
    monkeypatch.setattr(firmware.time, "monotonic", lambda: now[0])
    src = FirmwareSource(FakeSession(fail=True))
    assert await src.async_history() == {} and src._session.calls == 1
    now[0] += 30
    await src.async_history()
    assert src._session.calls == 1  # inside the 60 s backoff
    now[0] += 40
    await src.async_history()
    assert src._session.calls == 2
    now[0] += 400  # past the 5 min backoff, the network is back
    src._session = FakeSession()
    assert 0x59 in await src.async_history() and src._history_failures == 0


async def test_an_entity_created_before_the_fetch_shows_the_notes_afterwards(hass: HomeAssistant, enable_bluetooth):
    old = bytes([0x55, 0x57]) + SAMPLE[2:11] + bytes([0, 0])
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=old), hw=0, version=0x57)  # history empty at setup
    try:
        entity = hass.data[DATA_COMPONENT].get_entity("update.atc_000001")
        assert entity.release_summary is None
        firmware_source = entity.hass.data["pvvx_ota"].source
        firmware_source._history = parse_history(README)  # a fetch (by any entity) succeeded meanwhile
        assert entity.release_summary.startswith("Fixed an error during the initial OTA download")
        assert "reconfiguration" in await entity.async_release_notes()
    finally:
        [c.stop() for c in ctx]


async def test_up_to_date_sensor_gets_its_own_notes_not_the_fallback(hass: HomeAssistant, enable_bluetooth):
    current = bytes([0x55, 0x59]) + SAMPLE[2:11] + bytes([0, 0])
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=current), hw=0, version=0x59)
    try:
        entity = hass.data[DATA_COMPONENT].get_entity("update.atc_000001")
        assert entity._is_current(entity._target())
        assert await entity.async_release_notes() is None  # no history yet: nothing to say, but no "not found" text
        assert entity.release_summary is None
        entity.hass.data["pvvx_ota"].source._history = parse_history(README)
        assert entity.release_summary.startswith("Fixed an error during the initial OTA download")
        notes = await entity.async_release_notes()
        assert "V5.9" in notes and "installed, current" in notes and "No release notes were found" not in notes
        entity._version = None  # unknown installed version: not "current", the offered version's notes are shown
        assert not entity._is_current(entity._target()) and "V5.9" in await entity.async_release_notes()
    finally:
        [c.stop() for c in ctx]
