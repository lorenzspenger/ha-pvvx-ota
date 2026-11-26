"""Config flow and update entity tests."""

from contextlib import asynccontextmanager, contextmanager
from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pvvx_ota import ota
from custom_components.pvvx_ota.const import DOMAIN
from custom_components.pvvx_ota.errors import ha_error
from custom_components.pvvx_ota.firmware import FirmwareSource, parse_catalog

from .fake_gatt import FakeClient
from .test_ota import CATALOG, image
from .translated import raises_key

MAC = "A4:C1:38:00:00:01"


def svc(name="ATC_000001", address=MAC, service_data=None):
    dev = BLEDevice(address, name, {})
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=-60,
        manufacturer_data={},
        service_data=service_data or {},
        service_uuids=[],
        source="local",
        device=dev,
        advertisement=None,
        connectable=True,
        time=0,
        tx_power=None,
    )


BTHOME = {"0000fcd2-0000-1000-8000-00805f9b34fb": b"\x41\x01\x32"}


async def fast_read_cfg(client):
    return await ota.read_cfg(client, timeout=0.05)


FLOW_CONN = "custom_components.pvvx_ota.config_flow.async_connected"


def flow_conn(client):
    @asynccontextmanager
    async def fake(hass, address, name, **kwargs):
        if client is None:
            raise HomeAssistantError("not connectable")
        try:
            yield client
        finally:
            await client.disconnect()

    return patch(FLOW_CONN, fake)


async def test_flow_bluetooth_confirmed_by_connecting(hass: HomeAssistant, enable_bluetooth):
    r = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc(service_data=BTHOME)
    )
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "bluetooth_confirm"
    client = FakeClient(cfg=bytes([0x55, 0x58, 0, 0, 0, 0, 10, 4, 0x8B, 0, 55, 1]))
    with flow_conn(client), patch("custom_components.pvvx_ota.async_setup_entry", return_value=True):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.CREATE_ENTRY and r["title"] == "ATC_000001 (00:00:01)"
    assert r["data"] == {CONF_ADDRESS: MAC, "installed_version": 0x58, "hw_id": 1}
    assert client.disconnected


async def test_flow_bthome_non_pvvx_rejected(hass: HomeAssistant, enable_bluetooth):
    # Shelly BLU style: BTHome UUID but no pvvx hint -> never offered
    r = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_BLUETOOTH},
        data=svc(name="SBBT-002C", address="11:22:33:44:55:66", service_data=BTHOME),
    )
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "not_pvvx", r
    # hinted by MAC prefix but the device is not pvvx (no 0x55 reply) -> rejected after the connection
    r = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc(name="", service_data=BTHOME)
    )
    assert r["step_id"] == "bluetooth_confirm"
    with (
        flow_conn(FakeClient(no_reply=True)),
        patch("custom_components.pvvx_ota.ota.CFG_TIMEOUT", 0.05),
        patch("custom_components.pvvx_ota.config_flow.read_cfg", fast_read_cfg),
    ):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "not_pvvx", r


async def test_flow_encrypted_advert_without_name(hass: HomeAssistant, enable_bluetooth):
    """Encrypted BTHome: no name (passive proxy), opaque payload; MAC prefix hint + connection proves it."""
    enc = {"0000fcd2-0000-1000-8000-00805f9b34fb": b"\x41\xde\xad\xbe\xef\x00\x01\x02\x03\x04\x05\x06"}
    r = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc(name="", service_data=enc)
    )
    assert r["step_id"] == "bluetooth_confirm"
    assert r["description_placeholders"]["name"] == MAC
    with flow_conn(FakeClient()), patch("custom_components.pvvx_ota.async_setup_entry", return_value=True):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.CREATE_ENTRY and r["title"] == MAC


async def test_flow_cannot_connect_allows_retry(hass: HomeAssistant, enable_bluetooth):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    with flow_conn(None):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": "cannot_connect"}
    with flow_conn(FakeClient()), patch("custom_components.pvvx_ota.async_setup_entry", return_value=True):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.CREATE_ENTRY


async def test_flow_duplicate(hass: HomeAssistant, enable_bluetooth):
    MockConfigEntry(domain=DOMAIN, unique_id=MAC, data={CONF_ADDRESS: MAC}).add_to_hass(hass)
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "already_configured"


async def test_flow_user(hass: HomeAssistant, enable_bluetooth):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert r["reason"] == "no_devices_found"
    found = [svc(), svc(name="SBBT-002C", address="11:22:33:44:55:66", service_data=BTHOME)]
    with patch("custom_components.pvvx_ota.config_flow.async_discovered_service_info", return_value=found):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        assert r["type"] is FlowResultType.FORM
        assert len(r["data_schema"].schema[CONF_ADDRESS].container) == 2  # unverified BTHome listed too
        with flow_conn(FakeClient()), patch("custom_components.pvvx_ota.async_setup_entry", return_value=True):
            r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_ADDRESS: MAC})
    assert r["type"] is FlowResultType.CREATE_ENTRY


def test_manifest_matchers():
    import json
    from pathlib import Path

    m = json.loads(Path("custom_components/pvvx_ota/manifest.json").read_text())
    uuids = {x.get("service_data_uuid") for x in m["bluetooth"]}
    assert "0000fcd2-0000-1000-8000-00805f9b34fb" in uuids and "0000181a-0000-1000-8000-00805f9b34fb" in uuids
    assert all(x["connectable"] for x in m["bluetooth"])


# -- update entity ------------------------------------------------------
def patch_catalog(cat=None):
    cat = cat or parse_catalog(CATALOG)

    async def _get(self):
        return cat

    async def _dl(self, path):
        return image(500)

    return (
        patch.object(FirmwareSource, "async_catalog", _get),
        patch.object(FirmwareSource, "async_download", _dl),
    )


def patch_connection(client: FakeClient | None, calls: list | None = None):
    @asynccontextmanager
    async def fake(hass, address, name):
        if client is None:
            raise ha_error("not_connectable", name=name, address=MAC, reason="not heard")
        try:
            yield client
        finally:
            await client.disconnect()

    @contextmanager
    def both():
        with (
            patch("custom_components.pvvx_ota.update.async_connected", fake),
            patch("custom_components.pvvx_ota.store.async_connected", fake),
        ):
            yield

    return both()


async def setup_entry(hass: HomeAssistant, client, version=None):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, title="ATC_000001", data={CONF_ADDRESS: MAC})
    entry.add_to_hass(hass)
    p1, p2 = patch_catalog()
    with p1, p2, patch_connection(client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


ENTITY = "update.atc_000001"


async def test_state_update_available(hass: HomeAssistant, enable_bluetooth):
    # device reports 0x58 = V5.8, release is 89 = V5.9
    client = FakeClient(cfg=bytes([0x55, 0x58, 0, 0, 0, 0, 10, 4, 0x8B, 0, 55, 0]))
    await setup_entry(hass, client)
    st = hass.states.get(ENTITY)
    assert st.state == "on"
    assert st.attributes["installed_version"] == "5.8" and st.attributes["latest_version"] == "5.9"
    assert st.attributes["board"] == "ATC" and st.attributes["hardware_id"] == 0
    assert client.disconnected


async def test_state_up_to_date(hass: HomeAssistant, enable_bluetooth):
    await setup_entry(hass, FakeClient())
    assert hass.states.get(ENTITY).state == "off"


@pytest.mark.unheard
async def test_sensor_not_heard_by_any_scanner_means_a_retry_and_no_entities(hass: HomeAssistant, enable_bluetooth):
    """test-before-setup: with no connectable scanner having seen the sensor the entry waits (HA retries)."""
    from homeassistant.config_entries import ConfigEntryState

    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, title="ATC_000001", data={CONF_ADDRESS: MAC})
    entry.add_to_hass(hass)
    p1, p2 = patch_catalog()
    with (
        p1,
        p2,
        patch_connection(None),
        patch("custom_components.pvvx_ota.store.ConfigStore.async_initial_read") as read,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert [e for e in er.async_get(hass).entities.values() if e.platform == DOMAIN] == []
    assert hass.states.get(ENTITY) is None
    assert not read.called  # and no connection was tried


async def test_install_flashes_and_disconnects(hass: HomeAssistant, enable_bluetooth):
    client = FakeClient(cfg=bytes([0x55, 0x58, 0, 0, 0, 0, 10, 4, 0x8B, 0, 55, 0]))
    await setup_entry(hass, client)
    client.disconnected = False
    p1, p2 = patch_catalog()
    with p1, p2, patch_connection(client), patch("custom_components.pvvx_ota.update.POST_FLASH_DELAY", 3600):
        await hass.services.async_call("update", "install", {"entity_id": ENTITY}, blocking=True)
    assert client.writes[0] == bytes.fromhex("00ff") and client.writes[-1][:2] == b"\xff"[:0] + b"\x02\xff"
    assert client.disconnected
    assert hass.states.get(ENTITY).attributes["in_progress"] is False


async def test_install_failure_disconnects(hass: HomeAssistant, enable_bluetooth):
    client = FakeClient(cfg=bytes([0x55, 0x58, 0, 0, 0, 0, 10, 4, 0x8B, 0, 55, 0]), fail_status_at=3)
    await setup_entry(hass, client)
    client.disconnected = False
    p1, p2 = patch_catalog()
    with p1, p2, patch_connection(client), pytest.raises(HomeAssistantError, match="CRC"):
        await hass.services.async_call("update", "install", {"entity_id": ENTITY}, blocking=True)
    assert client.disconnected
    assert hass.states.get(ENTITY).attributes["in_progress"] is False


async def test_install_refuses_unknown_hw(hass: HomeAssistant, enable_bluetooth):
    # hw id 2 maps to "?" in the catalog
    client = FakeClient(cfg=bytes([0x55, 0x58, 0, 0, 0, 0, 10, 4, 0x8B, 0, 55, 2]))
    await setup_entry(hass, client)
    p1, p2 = patch_catalog()
    with p1, p2, patch_connection(client), pytest.raises(HomeAssistantError, match="hardware id"):
        await hass.services.async_call("update", "install", {"entity_id": ENTITY}, blocking=True)
    assert client.writes == [] and client.disconnected


async def test_install_not_connectable(hass: HomeAssistant, enable_bluetooth):
    await setup_entry(hass, FakeClient(cfg=bytes([0x55, 0x58, 0, 0, 0, 0, 10, 4, 0x8B, 0, 55, 0])))
    p1, p2 = patch_catalog()
    with p1, p2, patch_connection(None), raises_key("not_connectable"):
        await hass.services.async_call("update", "install", {"entity_id": ENTITY}, blocking=True)


async def test_flash_file_board_mismatch_and_allowlist(hass: HomeAssistant, enable_bluetooth, tmp_path):
    client = FakeClient()
    await setup_entry(hass, client)
    bad = tmp_path / "MHO_C401_v59.bin"
    bad.write_bytes(image())
    p1, p2 = patch_catalog()
    with p1, p2, patch_connection(client):
        with raises_key("path_not_allowed", ServiceValidationError):
            await hass.services.async_call(DOMAIN, "flash_file", {"entity_id": ENTITY, "path": str(bad)}, blocking=True)
        hass.config.allowlist_external_dirs.add(str(tmp_path))
        with pytest.raises(HomeAssistantError, match="board"):  # device is hw 0 = ATC
            await hass.services.async_call(DOMAIN, "flash_file", {"entity_id": ENTITY, "path": str(bad)}, blocking=True)
        good = tmp_path / "ATC_v5a.bin"
        good.write_bytes(image())
        with patch("custom_components.pvvx_ota.update.POST_FLASH_DELAY", 3600):
            await hass.services.async_call(
                DOMAIN, "flash_file", {"entity_id": ENTITY, "path": str(good)}, blocking=True
            )
    assert client.writes[-1][:2] == b"\x02\xff"


async def test_a_second_flash_waits_for_the_lock_instead_of_being_refused(hass: HomeAssistant, enable_bluetooth):
    """Used to be refused ("one at a time"); now it queues (see tests/test_queue.py for the full behaviour)."""
    client = FakeClient(cfg=bytes([0x55, 0x58, 0, 0, 0, 0, 10, 4, 0x8B, 0, 55, 0]))
    await setup_entry(hass, client)
    import asyncio

    from custom_components.pvvx_ota import get_shared

    async with get_shared(hass).lock:
        task = hass.async_create_task(
            hass.services.async_call("update", "install", {"entity_id": ENTITY}, blocking=True)
        )
        await asyncio.sleep(0.05)
        assert not task.done()  # waiting, not refused
        assert hass.states.get(ENTITY).attributes["in_progress"] is True
        assert hass.states.get(ENTITY).attributes["queued_position"] == 1
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).attributes["in_progress"] is False  # cancelled cleanly
    assert "queued_position" not in hass.states.get(ENTITY).attributes


# -- device registry: attach to the BTHome device -------------------------------
def bthome_device(hass: HomeAssistant):
    bt = MockConfigEntry(domain="bthome", title="ATC 000001")
    bt.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=bt.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, MAC)},
        name="ATC 000001",
        model="LYWSD03MMC",
        manufacturer="Xiaomi",
    )
    return bt, dev


def our_device(hass):
    ent = er.async_get(hass).async_get(ENTITY)
    assert ent is not None and ent.unique_id == f"{MAC}_firmware"
    return dr.async_get(hass).async_get(ent.device_id)


def owned_by(hass, entry):
    return dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)


def entity_device_id(hass):
    return er.async_get(hass).async_get(ENTITY).device_id


async def test_entity_attached_to_bthome_device(hass: HomeAssistant, enable_bluetooth):
    """HA >= 2026.10: a device has one config entry, so the entity sits on BTHome's device."""
    bt, dev = bthome_device(hass)
    entry = await setup_entry(hass, FakeClient())
    assert entity_device_id(hass) == dev.id
    assert owned_by(hass, entry) == []  # we created no device of our own
    assert dr.async_get(hass).async_get(dev.id) == dev  # untouched
    assert er.async_get(hass).async_get(ENTITY).unique_id == f"{MAC}_firmware"


async def test_own_device_without_bthome(hass: HomeAssistant, enable_bluetooth):
    entry = await setup_entry(hass, FakeClient())
    ours = owned_by(hass, entry)
    assert len(ours) == 1 and entity_device_id(hass) == ours[0].id
    assert (dr.CONNECTION_BLUETOOTH, MAC) in ours[0].connections and ours[0].manufacturer == "pvvx"


async def test_relinked_when_bthome_appears_and_disappears(hass: HomeAssistant, enable_bluetooth):
    entry = await setup_entry(hass, FakeClient())
    own_id = entity_device_id(hass)
    p1, p2 = patch_catalog()
    with p1, p2, patch_connection(FakeClient()):
        bt, dev = bthome_device(hass)
        await hass.async_block_till_done()
        assert entity_device_id(hass) == dev.id
        assert owned_by(hass, entry) == []  # the old own device was cleaned up
        assert dr.async_get(hass).async_get(own_id) is None
        # BTHome device removed again -> back to an own device
        dr.async_get(hass).async_remove_device(dev.id)
        await hass.async_block_till_done()
        ours = owned_by(hass, entry)
        assert len(ours) == 1 and entity_device_id(hass) == ours[0].id


async def test_old_own_device_cleaned_up_on_setup(hass: HomeAssistant, enable_bluetooth):
    """Upgrade case: an earlier version left an own device next to the BTHome device."""
    bt, dev = bthome_device(hass)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, title="ATC_000001", data={CONF_ADDRESS: MAC})
    entry.add_to_hass(hass)
    stale = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, connections={(dr.CONNECTION_BLUETOOTH, MAC)}, name="old"
    )
    p1, p2 = patch_catalog()
    with p1, p2, patch_connection(FakeClient()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert dr.async_get(hass).async_get(stale.id) is None
    assert entity_device_id(hass) == dev.id


async def test_removing_our_entry_keeps_bthome_device(hass: HomeAssistant, enable_bluetooth):
    bt, dev = bthome_device(hass)
    entry = await setup_entry(hass, FakeClient())
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert dr.async_get(hass).async_get(dev.id) == dev
    assert er.async_get(hass).async_get(ENTITY) is None


async def test_flow_pin_sensor_becomes_an_ignored_entry(hass: HomeAssistant, enable_bluetooth):
    """A PIN sensor is never added: it is ignored (like HA's Ignore button), so discovery stops offering it."""
    from homeassistant.config_entries import SOURCE_IGNORE

    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    client = FakeClient(needs_pairing=True)
    with flow_conn(client):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "pin_protected"
    assert client.pair_calls == 0
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1 and entries[0].source == SOURCE_IGNORE and entries[0].unique_id == MAC
    assert entries[0].disabled_by is None and entries[0].data == {}
    # discovered again: not offered
    r2 = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    assert r2["type"] is FlowResultType.ABORT and r2["reason"] == "already_configured"
    # not in the manual picker either
    with patch("custom_components.pvvx_ota.config_flow.async_discovered_service_info", return_value=[svc()]):
        r3 = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert r3["type"] is FlowResultType.ABORT and r3["reason"] == "no_devices_found"


async def test_unignoring_after_removing_the_pin_allows_adding(hass: HomeAssistant, enable_bluetooth):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    with flow_conn(FakeClient(needs_pairing=True)):
        await hass.config_entries.flow.async_configure(r["flow_id"], {})
    (ignored,) = hass.config_entries.async_entries(DOMAIN)
    assert await hass.config_entries.async_remove(ignored.entry_id)  # "un-ignore" = delete the ignored entry
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "bluetooth_confirm"  # offered again
    with flow_conn(FakeClient()), patch("custom_components.pvvx_ota.async_setup_entry", return_value=True):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.CREATE_ENTRY


async def test_manual_pick_of_a_pin_sensor_is_refused_without_ignoring_it(hass: HomeAssistant, enable_bluetooth):
    """Only discovery ignores a PIN sensor; a sensor the user picked by hand just gets the explanation."""
    with patch("custom_components.pvvx_ota.config_flow.async_discovered_service_info", return_value=[svc()]):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        with flow_conn(FakeClient(needs_pairing=True)):
            r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_ADDRESS: MAC})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "pin_protected_manual"
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_connection_failure_does_not_ignore(hass: HomeAssistant, enable_bluetooth):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    with flow_conn(None):  # could not connect: not a PIN, nothing is remembered
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": "cannot_connect"}
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_flow_unexpected_error_is_reported(hass: HomeAssistant, enable_bluetooth):
    class Boom(FakeClient):
        async def start_notify(self, char, cb):
            raise ValueError("boom")

    # a non-pairing, non-bleak failure inside read_cfg is "not pvvx"; failures elsewhere are "unexpected"
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    with (
        flow_conn(FakeClient()),
        patch("custom_components.pvvx_ota.config_flow.read_cfg", side_effect=RuntimeError("kaboom")),
    ):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": "unexpected"}
    assert "kaboom" in r["description_placeholders"]["error"]


# -- display names in the setup flow --------------------------------------------
def _add_bthome(hass, mac, name, name_by_user=None):
    bt = MockConfigEntry(domain="bthome", title=name)
    bt.add_to_hass(hass)
    reg = dr.async_get(hass)
    dev = reg.async_get_or_create(config_entry_id=bt.entry_id, connections={(dr.CONNECTION_BLUETOOTH, mac)}, name=name)
    if name_by_user:
        dev = reg.async_update_device(dev.id, name_by_user=name_by_user)
    return dev


async def test_display_name_registry_name(hass: HomeAssistant, enable_bluetooth):
    from custom_components.pvvx_ota.config_flow import display_name

    _add_bthome(hass, MAC, "ATC Sensor 1")
    assert display_name(hass, svc()) == "ATC Sensor 1 (00:00:01)"
    r = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc(service_data=BTHOME)
    )
    assert r["description_placeholders"]["name"] == "ATC Sensor 1 (00:00:01)"
    flow = next(f for f in hass.config_entries.flow.async_progress() if f["flow_id"] == r["flow_id"])
    assert flow["context"]["title_placeholders"] == {"name": "ATC Sensor 1 (00:00:01)"}
    with flow_conn(FakeClient()), patch("custom_components.pvvx_ota.async_setup_entry", return_value=True):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["title"] == "ATC Sensor 1 (00:00:01)"


async def test_display_name_user_renamed(hass: HomeAssistant, enable_bluetooth):
    from custom_components.pvvx_ota.config_flow import display_name

    _add_bthome(hass, MAC, "ATC 000001", name_by_user="Bedroom sensor")
    assert display_name(hass, svc()) == "Bedroom sensor (00:00:01)"


async def test_display_name_no_registry_entry(hass: HomeAssistant, enable_bluetooth):
    from custom_components.pvvx_ota.config_flow import display_name

    assert display_name(hass, svc(name="")) == MAC  # nothing known: the MAC
    assert display_name(hass, svc()) == "ATC_000001 (00:00:01)"  # advertised pvvx name


async def test_picker_same_name_distinguishable(hass: HomeAssistant, enable_bluetooth):
    other = "A4:C1:38:00:00:02"
    _add_bthome(hass, MAC, "ATC Sensor 2")
    _add_bthome(hass, other, "ATC Sensor 2")
    found = [svc(), svc(name="", address=other)]
    with patch("custom_components.pvvx_ota.config_flow.async_discovered_service_info", return_value=found):
        r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    labels = r["data_schema"].schema[CONF_ADDRESS].container
    assert labels == {MAC: "ATC Sensor 2 (00:00:01)", other: "ATC Sensor 2 (00:00:02)"}


async def test_connect_is_bounded_and_says_it_is_not_a_pin(hass: HomeAssistant, enable_bluetooth):
    import asyncio

    from custom_components.pvvx_ota import device

    async def hang(*args, **kwargs):
        await asyncio.sleep(3600)

    with (
        patch.object(device, "CONNECT_TIMEOUT", 0.05),
        patch.object(device, "establish_connection", hang),
        patch.object(device.bluetooth, "async_ble_device_from_address", return_value=BLEDevice(MAC, "x", {})),
    ):
        with raises_key("connect_timeout"):
            async with device.async_connected(hass, MAC, "ATC_000001"):
                pass


async def test_flow_reports_cannot_connect_instead_of_hanging(hass: HomeAssistant, enable_bluetooth):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    with flow_conn(None):  # connection failure
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": "cannot_connect"}
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_describe_link_names_the_connected_proxy_and_rssi(hass: HomeAssistant, enable_bluetooth):
    from types import SimpleNamespace

    from custom_components.pvvx_ota import device

    client = SimpleNamespace(_connected_scanner=SimpleNamespace(name="proxy-2", source="AA:BB:CC:00:00:02"))
    other = SimpleNamespace(
        scanner=SimpleNamespace(source="AA:BB:CC:00:00:03"), advertisement=SimpleNamespace(rssi=-50)
    )
    mine = SimpleNamespace(scanner=SimpleNamespace(source="AA:BB:CC:00:00:02"), advertisement=SimpleNamespace(rssi=-78))
    with patch.object(device.bluetooth, "async_scanner_devices_by_address", return_value=[other, mine]):
        assert device.describe_link(hass, client, MAC) == "proxy-2 (AA:BB:CC:00:00:02), RSSI -78"
    # RSSI lookup failing is fine, an undeterminable proxy is said so instead of guessed
    with patch.object(device.bluetooth, "async_scanner_devices_by_address", side_effect=RuntimeError):
        assert device.describe_link(hass, client, MAC).endswith("RSSI unknown")
    assert "unknown proxy" in device.describe_link(hass, SimpleNamespace(), MAC)


async def test_connect_logs_the_link(hass: HomeAssistant, enable_bluetooth, caplog):
    from types import SimpleNamespace

    from custom_components.pvvx_ota import device

    caplog.set_level("DEBUG")

    class C(SimpleNamespace):
        async def disconnect(self):
            pass

    async def fake_establish(*a, **k):
        return C(_connected_scanner=SimpleNamespace(name="proxy-1", source="AA:BB:CC:00:00:01"))

    with (
        patch.object(device, "establish_connection", fake_establish),
        patch.object(device.bluetooth, "async_ble_device_from_address", return_value=BLEDevice(MAC, "x", {})),
        patch.object(device.bluetooth, "async_scanner_devices_by_address", return_value=[]),
    ):
        async with device.async_connected(hass, MAC, "ATC_000001"):
            pass
    assert "Connected to ATC_000001" in caplog.text and "proxy-1 (AA:BB:CC:00:00:01)" in caplog.text
