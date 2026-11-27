"""Installing a specific firmware version (also an older one) with update.install: allowed versions, the checks before a
flash, the pinned images and the settings/key carry-over of a downgrade across the 5.7/5.8 EEPROM rewrite."""

import hashlib
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.update import UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.pvvx_ota import config as cfg
from custom_components.pvvx_ota import firmware_versions as fv
from custom_components.pvvx_ota.const import DOMAIN
from custom_components.pvvx_ota.firmware import (
    Catalog,
    FirmwareSource,
    installable_versions,
    pin_for,
    verify_pinned,
)
from custom_components.pvvx_ota.ota import FirmwareError

from .fake_gatt import FakeClient
from .test_config import SAMPLE, UPDATE, fast_batch, setup_cfg_entry  # noqa: F401
from .test_connection_and_firmware_errors import Resp, Session
from .test_eep_boundary import OLD_KEY, sensor
from .test_lifecycle import HW0, capture_verify
from .test_ota import image
from .translated import raises_key

PINNED = "custom_components.pvvx_ota.firmware.FirmwareSource.async_download_pinned"


async def install(hass, version=None, **extra):
    data = {"entity_id": UPDATE, **extra}
    if version is not None:
        data["version"] = version
    await hass.services.async_call("update", "install", data, blocking=True)


def sensor_hw(hw, version=0x59):
    """A sensor reporting hardware id ``hw`` (byte 11 of the config reply)."""
    cfg_bytes = bytearray(bytes([0x55, version]) + HW0[2:])
    cfg_bytes[11] = hw
    return FakeClient(cfg=bytes(cfg_bytes))


# -- the table and the allowed versions ------------------------------------------------------------


def test_the_generated_table_is_consistent():
    assert set(fv.RELEASE_COMMITS) == set(cfg.VERIFIED_VERSIONS)
    assert set(fv.IMAGES) <= set(cfg.VERIFIED_VERSIONS)
    for version, boards in fv.IMAGES.items():
        for board, entry in boards.items():
            sha, size = entry[0], entry[1]
            assert len(sha) == 64 and int(sha, 16) >= 0, (version, board)
            assert 0 < size <= 0x20000, (version, board)
            assert len(entry) in (2, 3) and (len(entry) == 2 or len(entry[2]) == 40)
            pin = pin_for(version, board)
            assert pin is not None and pin.url.startswith("https://raw.githubusercontent.com/pvvx/ATC_MiThermometer/")
            assert ("/bin/" in pin.url) == (version > fv.FLAT_UNTIL)
            assert ".." not in pin.url and pin.url.endswith(f"{board}_v{version:02x}.bin")
    assert fv.DEVICE_BYTE["ATC"] == 0x0A


def test_allowed_versions_follow_board_and_hardware():
    atc = installable_versions("ATC", 0)
    assert atc[0] == 0x42 and atc[-1] == 0x59 and 0x56 in atc and 0x57 in atc
    assert 0x52 not in installable_versions("ATC", 14) and 0x53 in installable_versions("ATC", 14)  # hw id 14: from 5.3
    assert installable_versions("MHO_C122", 11)[0] == 0x43  # the board has no earlier image
    assert installable_versions("BTE", 12)[0] == 0x49
    assert installable_versions(None, 0) == [] and installable_versions("NOPE", 0) == []
    assert installable_versions("ATC", None) == atc  # no hardware id: only the board limits it
    assert 0x41 not in atc and 0x60 not in atc  # only VERIFIED_VERSIONS


def test_pin_for_names_the_release_commit_and_a_flat_path_before_5_1():
    old, new = pin_for(0x42, "ATC"), pin_for(0x59, "ATC")
    assert old is not None and new is not None
    assert old.path == "ATC_v42.bin" and new.path == "bin/ATC_v59.bin"
    assert new.commit == fv.RELEASE_COMMITS[0x59] == "9b65a24a301913d5045f08ab438b012092421bab"
    assert pin_for(0x59, "NOPE") is None and pin_for(0x41, "ATC") is None
    assert new.url.endswith(f"/{new.commit}/bin/ATC_v59.bin")


# -- the bytes must be the pinned ones --------------------------------------------------------------


def pinned_image(device_byte=0x0A, version=0x57):
    fw = bytearray(image(300))
    fw[0x14] = device_byte
    fw = bytes(fw)
    entry = (hashlib.sha256(fw).hexdigest(), len(fw))
    return fw, entry


def test_verify_pinned_accepts_only_the_pinned_bytes():
    fw, entry = pinned_image()
    with patch.dict(fv.IMAGES, {0x57: {"ATC": entry}}):
        pin = pin_for(0x57, "ATC")
        assert pin is not None
        verify_pinned(fw, pin)
        flipped = bytearray(fw)
        flipped[100] ^= 1
        for bad in (bytes(flipped), fw + b"\x00", fw[:-16], b""):
            with pytest.raises(FirmwareError, match="pinned checksum"):
                verify_pinned(bad, pin)


def test_verify_pinned_checks_the_header_board_byte_from_4_7_on():
    fw, entry = pinned_image(device_byte=0x02)  # a CGG1 image under the ATC name
    with patch.dict(fv.IMAGES, {0x57: {"ATC": entry}, 0x46: {"ATC": entry}}):
        pin = pin_for(0x57, "ATC")
        assert pin is not None
        with pytest.raises(FirmwareError, match="not that of board ATC"):
            verify_pinned(fw, pin)
        old = pin_for(0x46, "ATC")
        assert old is not None
        verify_pinned(fw, old)  # before 4.7 the byte is not looked at


def test_verify_pinned_still_requires_a_telink_image():
    junk = b"\x01" * 400
    with patch.dict(fv.IMAGES, {0x57: {"ATC": (hashlib.sha256(junk).hexdigest(), len(junk))}}):
        pin = pin_for(0x57, "ATC")
        assert pin is not None
        with pytest.raises(FirmwareError, match="KNLT"):
            verify_pinned(junk, pin)


async def test_the_pinned_download_is_fetched_by_commit_and_verified():
    fw, entry = pinned_image()
    with patch.dict(fv.IMAGES, {0x57: {"ATC": entry}}):
        pin = pin_for(0x57, "ATC")
        assert pin is not None
        session = Session(Resp(chunks=(fw[:100], fw[100:])))
        urls = []
        session.get = lambda url, **k: urls.append(url) or session.resp  # type: ignore[method-assign]
        assert await FirmwareSource(session).async_download_pinned(pin) == fw
        assert urls == [pin.url] and fv.RELEASE_COMMITS[0x57] in urls[0]
        with pytest.raises(FirmwareError, match="pinned checksum"):
            await FirmwareSource(Session(Resp(chunks=(fw[:-1] + b"\x00",)))).async_download_pinned(pin)


# -- update.install with a version -----------------------------------------------------------------


async def test_a_specific_older_version_is_flashed_from_its_pinned_image(
    hass: HomeAssistant, enable_bluetooth, fast_batch, caplog
):
    client = FakeClient(cfg=HW0)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x59)
    try:
        fw = image(900)
        with patch(PINNED, AsyncMock(return_value=fw)) as pinned, capture_verify()[1]:
            await install(hass, " v5.5 ")
        (pin,) = pinned.call_args.args
        assert (pin.version, pin.board) == (0x55, "ATC") and pin.path == "bin/ATC_v55.bin"
        assert pin.commit == fv.RELEASE_COMMITS[0x55]
        assert client.marker_sent
        assert "older than the installed one" in caplog.text
        assert UpdateEntityFeature.SPECIFIC_VERSION in UpdateEntityFeature(
            hass.states.get(UPDATE).attributes["supported_features"]
        )
    finally:
        [c.stop() for c in ctx]


async def test_the_installed_version_can_be_chosen_again(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=bytes([0x55, 0x57]) + HW0[2:])
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x57)
    try:
        with patch(PINNED, AsyncMock(return_value=image(500))) as pinned, capture_verify()[1]:
            await install(hass, "5.7")
        assert pinned.call_args.args[0].version == 0x57 and client.marker_sent
    finally:
        [c.stop() for c in ctx]


@pytest.mark.parametrize("version", ["4.1", "4.0", "5.10", "6.0", "9.9", "0.0"])
async def test_versions_outside_the_verified_list_are_refused_with_the_allowed_ones(
    hass: HomeAssistant, enable_bluetooth, fast_batch, version
):
    client = FakeClient(cfg=HW0)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        before = len(calls)
        with patch(PINNED, AsyncMock()) as pinned:
            with raises_key("version_not_available", ServiceValidationError, board="ATC") as info:
                await install(hass, version)
        assert "4.2, 4.3" in info.value.translation_placeholders["allowed"]
        assert info.value.translation_placeholders["allowed"].endswith("5.9")
        assert not pinned.called and not client.writes and len(calls) == before  # refused before any connection
    finally:
        [c.stop() for c in ctx]


@pytest.mark.parametrize("text", ["abc", "", "5", "5.7.1", "5,7", "latest"])
async def test_text_that_is_no_version_is_refused(hass: HomeAssistant, enable_bluetooth, fast_batch, text):
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=HW0), hw=0)
    try:
        with raises_key("version_format", ServiceValidationError, version=text):
            await install(hass, text)
    finally:
        [c.stop() for c in ctx]


async def test_a_version_older_than_the_hardware_knows_is_refused(hass: HomeAssistant, enable_bluetooth, fast_batch):
    """Hardware id 14 (LYWSD03MMC B1.6, 2025) is not in the firmware before 5.3."""
    client = sensor_hw(14)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=14)
    try:
        catalog = Catalog(version=0x59, beta=None, images=("bin/ATC_v59.bin",) * 15)
        with patch.object(FirmwareSource, "async_catalog", AsyncMock(return_value=catalog)):
            with raises_key("version_not_available", ServiceValidationError, version="5.2") as info:
                await install(hass, "5.2")
            assert info.value.translation_placeholders["allowed"].startswith("5.3, 5.4")
            with patch(PINNED, AsyncMock(return_value=image(500))), capture_verify()[1]:
                await install(hass, "5.3")
    finally:
        [c.stop() for c in ctx]


async def test_the_hardware_id_read_from_the_sensor_is_checked_again(hass: HomeAssistant, enable_bluetooth, fast_batch):
    """The stored hardware id says 0; the sensor answers 14: refused inside the connection, nothing is sent."""
    client = sensor_hw(14)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        hass.data["entity_components"]["update"].get_entity(UPDATE)._hw_id = 0  # (the first read stored the real one)
        catalog = Catalog(version=0x59, beta=None, images=("bin/ATC_v59.bin",) * 15)
        with (
            patch.object(FirmwareSource, "async_catalog", AsyncMock(return_value=catalog)),
            patch(PINNED, AsyncMock()) as pinned,
            raises_key("version_not_available", ServiceValidationError, version="5.2"),
        ):
            await install(hass, "5.2")
        assert not pinned.called and not client.writes and not client.marker_sent
        assert entry.runtime_data.last_flash["result"] == "failed before the transfer"
    finally:
        [c.stop() for c in ctx]


async def test_a_sensor_without_a_known_board_cannot_get_a_specific_version(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=HW0), hw=0)
    try:
        hass.data["entity_components"]["update"].get_entity(UPDATE)._hw_id = 2  # "?" in the test catalog: no BLE image
        with raises_key("version_board_unknown", ServiceValidationError):
            await install(hass, "5.7")
    finally:
        [c.stop() for c in ctx]


async def test_a_missing_firmware_list_refuses_a_specific_version(hass: HomeAssistant, enable_bluetooth, fast_batch):
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=HW0), hw=0)
    try:
        with (
            patch.object(FirmwareSource, "async_catalog", AsyncMock(side_effect=FirmwareError("offline"))),
            raises_key("catalog_needed_for_version", ServiceValidationError, error="offline"),
        ):
            await install(hass, "5.7")
    finally:
        [c.stop() for c in ctx]


async def test_a_download_that_does_not_match_the_pin_is_never_flashed(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = FakeClient(cfg=HW0)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        with (
            patch.object(FirmwareSource, "_fetch", AsyncMock(return_value=image(900))),
            raises_key("flash_failed") as info,
        ):
            await install(hass, "5.7")
        assert "pinned checksum" in info.value.translation_placeholders["error"]
        assert not client.writes and not client.marker_sent
        assert entry.runtime_data.last_flash["result"].startswith("failed")
    finally:
        [c.stop() for c in ctx]


async def test_the_battery_check_applies_and_force_overrides_it(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=HW0)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        entity = hass.data["entity_components"]["update"].get_entity(UPDATE)
        with (
            patch.object(type(entity), "_battery_voltage", return_value=2.0),
            patch(PINNED, AsyncMock(return_value=image(500))),
        ):
            with raises_key("battery_low"):
                await install(hass, "5.7")
            assert not client.writes
            with capture_verify()[1]:
                await hass.services.async_call(
                    DOMAIN, "flash_latest", {"entity_id": UPDATE, "version": "5.7", "force": True}, blocking=True
                )
        assert client.marker_sent
    finally:
        [c.stop() for c in ctx]


# -- the latest path stays as it was ----------------------------------------------------------------


async def test_install_without_a_version_is_the_latest_release_as_before(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = FakeClient(cfg=bytes([0x55, 0x57]) + HW0[2:])
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x57)
    try:
        with (
            patch(PINNED, AsyncMock()) as pinned,
            patch.object(FirmwareSource, "async_download", AsyncMock(return_value=image(500))) as latest,
            capture_verify()[1],
        ):
            await install(hass)
        assert latest.call_args.args == ("bin/ATC_v59.bin",) and not pinned.called and client.marker_sent
    finally:
        [c.stop() for c in ctx]


async def test_asking_for_the_latest_version_is_the_latest_path_too(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=bytes([0x55, 0x57]) + HW0[2:])
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x57)
    try:
        with (
            patch(PINNED, AsyncMock()) as pinned,
            patch.object(FirmwareSource, "async_download", AsyncMock(return_value=image(500))) as latest,
            capture_verify()[1],
        ):
            await install(hass, "5.9")
        assert latest.call_args.args == ("bin/ATC_v59.bin",) and not pinned.called
    finally:
        [c.stop() for c in ctx]


# -- a downgrade across the 5.7/5.8 EEPROM rewrite --------------------------------------------------


async def test_a_downgrade_across_the_boundary_carries_key_and_settings(
    hass: HomeAssistant, enable_bluetooth, fast_batch, caplog
):
    client = sensor(version=0x59, flash_to=0x57)  # encryption on, settings that differ from the defaults after an erase
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x59)
    try:
        started, timer = capture_verify()
        with patch(PINNED, AsyncMock(return_value=image(900))) as pinned, timer:
            await install(hass, "5.7")
        assert pinned.call_args.args[0].version == 0x57
        assert (
            client.cfg[1] == 0x57 and client.bindkey != OLD_KEY
        )  # the old firmware erased the EEPROM and made a new key
        assert client.cfg[2 + 4] == 50 and cfg.get(client.cfg, "adv_crypto") == 0
        store = entry.runtime_data
        assert store._carry_key == OLD_KEY  # in memory only, until the check has run
        entity = hass.data["entity_components"]["update"].get_entity(UPDATE)
        await entity._async_verify_after_flash()
        assert client.bindkey == OLD_KEY and client.key_writes == 1
        assert cfg.get(client.cfg, "adv_crypto") == 1
        assert cfg.get(client.cfg, "adv_interval") == cfg.get(HW0, "adv_interval") == 16
        assert store._carry_key is None and "pending_key" not in store.snapshot
        assert OLD_KEY.hex() not in caplog.text and "carried over" in caplog.text
    finally:
        [c.stop() for c in ctx]


async def test_an_upgrade_back_across_the_boundary_carries_them_as_well(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = sensor(version=0x57, flash_to=0x58)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x57)
    try:
        started, timer = capture_verify()
        with patch(PINNED, AsyncMock(return_value=image(900))), timer:
            await install(hass, "5.8")
        entity = hass.data["entity_components"]["update"].get_entity(UPDATE)
        await entity._async_verify_after_flash()
        assert client.cfg[1] == 0x58 and client.bindkey == OLD_KEY and cfg.get(client.cfg, "adv_crypto") == 1
    finally:
        [c.stop() for c in ctx]


async def test_a_downgrade_inside_one_side_of_the_boundary_does_not_touch_the_key(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = sensor(version=0x59, flash_to=0x58)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x59)
    try:
        started, timer = capture_verify()
        with patch(PINNED, AsyncMock(return_value=image(900))), timer:
            await install(hass, "5.8")
        assert entry.runtime_data._carry_key is None and client.key_reads == 0
    finally:
        [c.stop() for c in ctx]
