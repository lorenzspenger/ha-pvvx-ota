"""The firmware list survives a restart (stored copy), and a local image can be flashed without a board check on request."""

import json
import logging
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pvvx_ota import firmware
from custom_components.pvvx_ota.const import CONF_HW_ID, CONF_VERSION, DOMAIN
from custom_components.pvvx_ota.firmware import STORAGE_KEY, STORAGE_VERSION, FirmwareError, FirmwareSource

from .fake_gatt import FakeClient
from .test_ha import ENTITY, MAC, patch_catalog, patch_connection, setup_entry
from .test_ota import CATALOG, image
from .test_release_notes import README
from .translated import raises_key

REAL_HISTORY = FirmwareSource.async_history  # captured before the autouse stub replaces it
STRINGS = json.loads(Path("custom_components/pvvx_ota/strings.json").read_text())


@pytest.fixture
def real_history(monkeypatch):
    monkeypatch.setattr(FirmwareSource, "async_history", REAL_HISTORY)


class Resp:
    def __init__(self, data):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    async def json(self, content_type=None):
        return self.data

    async def text(self):
        return self.data


class Net:
    """GitHub, or its absence."""

    def __init__(self, up=True):
        self.up, self.calls = up, 0

    def get(self, url, **kw):
        self.calls += 1
        if not self.up:
            raise firmware.aiohttp.ClientError("no network")
        return Resp(CATALOG if url.endswith("firmware.json") else README)


def new_source(hass, net):
    return FirmwareSource(net, Store(hass, STORAGE_VERSION, STORAGE_KEY))


def preload(hass_storage, **data):
    hass_storage[STORAGE_KEY] = {"version": STORAGE_VERSION, "key": STORAGE_KEY, "data": data}


async def test_a_live_fetch_overwrites_the_store(hass: HomeAssistant, hass_storage, real_history):
    preload(hass_storage, catalog={**CATALOG, "version": 80}, fetched=1.0, history={})
    src = new_source(hass, Net())
    catalog = await src.async_catalog()
    await src.async_history()
    assert catalog.version == 89 and src.summary()["source"] == "live"
    stored = hass_storage[STORAGE_KEY]["data"]
    assert stored["catalog"]["version"] == 89 and stored["fetched"] > 1.0  # the old copy is gone
    assert stored["history"]["89"] and stored["history_fetched"]  # the release notes are kept too


async def test_the_stored_copy_is_the_catalog_when_github_is_unreachable(
    hass: HomeAssistant, hass_storage, caplog, real_history
):
    preload(
        hass_storage,
        catalog=CATALOG,
        fetched=time.time() - 3600,
        history={"89": "Fixed things"},
        history_fetched=time.time(),
    )
    caplog.set_level(logging.INFO)
    net = Net(up=False)
    src = new_source(hass, net)
    catalog = await src.async_catalog()  # the live fetch is tried first and fails, the stored copy answers
    assert net.calls == 1 and catalog.version == 89
    summary = src.summary()
    assert summary["source"] == "stored" and 3500 < summary["age_s"] < 3700 and summary["fetched_at"]
    assert (await src.async_history())[0x59] == "Fixed things"
    await src.async_catalog()
    assert caplog.text.count("using the copy stored") == 1  # told once, not on every retry
    net.up, src._attempted = True, float("-inf")
    await src.async_catalog()
    assert src.summary()["source"] == "live"  # the next live fetch replaces the stored copy


async def test_a_corrupt_store_is_ignored(hass: HomeAssistant, hass_storage):
    preload(hass_storage, catalog={"nonsense": 1}, fetched="x")
    src = new_source(hass, Net(up=False))
    with pytest.raises(FirmwareError, match="cannot fetch"):
        await src.async_catalog()
    assert src.summary()["loaded"] is False and src.summary()["source"] is None


async def test_without_a_store_nothing_is_persisted(hass: HomeAssistant, hass_storage):
    src = FirmwareSource(Net())
    await src.async_catalog()
    assert STORAGE_KEY not in hass_storage


async def test_updates_are_offered_from_the_stored_copy_while_github_is_down(
    hass: HomeAssistant, enable_bluetooth, hass_storage
):
    preload(hass_storage, catalog=CATALOG, fetched=time.time(), history={})
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=MAC, title="ATC_000001", data={CONF_ADDRESS: MAC, CONF_VERSION: 0x58, CONF_HW_ID: 0}
    )
    entry.add_to_hass(hass)
    client = FakeClient(cfg=bytes([0x55, 0x58, 0, 0, 0, 0, 10, 4, 0x8B, 0, 55, 0]))
    with (
        patch("custom_components.pvvx_ota.shared.async_get_clientsession", return_value=Net(up=False)),
        patch_connection(client),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state.state == "on" and state.attributes["latest_version"] == "5.9"  # installed 5.8, from the stored list


# -- flash_file: the board check, and skipping it on request ------------------------------------------


def no_catalog():
    async def _none(self):
        raise FirmwareError("cannot fetch firmware list: no network")

    return patch.object(FirmwareSource, "async_catalog", _none)


async def flash_file(hass, path, **extra):
    await hass.services.async_call(
        DOMAIN, "flash_file", {"entity_id": ENTITY, "path": str(path), **extra}, blocking=True
    )


@pytest.fixture
async def sensor(hass: HomeAssistant, enable_bluetooth, tmp_path):
    client = FakeClient()  # hardware id 0 = board ATC in the test catalog
    await setup_entry(hass, client)
    hass.config.allowlist_external_dirs.add(str(tmp_path))
    client.writes.clear()
    return client, tmp_path


def write(tmp, name, data=None):
    path = tmp / name
    path.write_bytes(image() if data is None else data)
    return path


async def test_without_any_catalog_a_local_file_is_refused_and_the_error_names_the_option(hass, sensor, caplog):
    client, tmp = sensor
    good = write(tmp, "ATC_v5a.bin")
    with (
        no_catalog(),
        patch_connection(client),
        raises_key("catalog_needed_for_file", ServiceValidationError, error="cannot fetch firmware list: no network"),
    ):
        await flash_file(hass, good)
    assert client.writes == []  # nothing was sent
    assert "skip_board_check" in STRINGS["exceptions"]["catalog_needed_for_file"]["message"]


async def test_without_a_catalog_the_skip_flag_flashes_but_every_other_check_still_applies(hass, sensor, caplog):
    client, tmp = sensor
    caplog.set_level(logging.WARNING)
    odd = write(tmp, "whatever_name.bin")  # the name says nothing about the board
    with (
        no_catalog(),
        patch_connection(client),
        patch("custom_components.pvvx_ota.flash_session.POST_FLASH_DELAY", 3600),
    ):
        await flash_file(hass, odd, skip_board_check=True)
    assert client.writes and client.writes[-1][:2] == b"\x02\xff"  # flashed to the end marker
    assert "WITHOUT checking" in caplog.text and "whatever_name.bin" in caplog.text and "ATC_000001" in caplog.text
    # the signature, size and header checks stay
    client.writes.clear()
    with no_catalog(), patch_connection(client), pytest.raises(Exception, match="KNLT|signature|Telink"):
        await flash_file(hass, write(tmp, "ATC_v5b.bin", b"junk" * 40), skip_board_check=True)
    assert client.writes == []


async def test_the_skip_flag_does_not_waive_the_battery_check(hass, sensor):
    client, tmp = sensor
    odd = write(tmp, "whatever_name.bin")
    entity = hass.data["entity_components"]["update"].get_entity(ENTITY)
    with (
        no_catalog(),
        patch_connection(client),
        patch.object(type(entity), "_battery_voltage", return_value=2.0),
        raises_key("battery_low", ServiceValidationError),
    ):
        await flash_file(hass, odd, skip_board_check=True)
    assert client.writes == []


async def test_a_wrong_board_prefix_is_flashed_with_the_skip_flag_and_a_warning(hass, sensor, caplog):
    client, tmp = sensor
    caplog.set_level(logging.WARNING)
    wrong = write(tmp, "MHO_C401_v59.bin")  # the sensor is an ATC
    p1, p2 = patch_catalog()
    with p1, p2, patch_connection(client):
        with pytest.raises(Exception, match="board"):  # without the flag: refused, as always
            await flash_file(hass, wrong)
        assert client.writes == []
        with patch("custom_components.pvvx_ota.flash_session.POST_FLASH_DELAY", 3600):
            await flash_file(hass, wrong, skip_board_check=True)
    assert client.writes and "WITHOUT checking" in caplog.text and "MHO_C401_v59.bin" in caplog.text


async def test_an_oversized_file_is_refused_before_it_is_read(hass, sensor):
    client, tmp = sensor
    big = tmp / "ATC_v5a.bin"
    big.write_bytes(b"\0" * 0x20001)
    p1, p2 = patch_catalog()
    with p1, p2, patch_connection(client), patch("pathlib.Path.read_bytes", side_effect=AssertionError("read")):
        with raises_key("file_too_big", ServiceValidationError, size="131073", limit="131072"):
            await flash_file(hass, big)
    assert client.writes == []


async def test_a_missing_file_is_unreadable(hass, sensor):
    client, tmp = sensor
    p1, p2 = patch_catalog()
    with p1, p2, patch_connection(client):
        with raises_key("file_unreadable", ServiceValidationError):
            await flash_file(hass, tmp / "ATC_v5a.bin")
