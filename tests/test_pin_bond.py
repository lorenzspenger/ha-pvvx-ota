"""PIN sensors with a bond in one proxy: encrypt before the first command, stay on that proxy, detect it in the flow."""

import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_IGNORE, ConfigEntryState
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pvvx_ota import bt_internals as bt
from custom_components.pvvx_ota import device as dev
from custom_components.pvvx_ota.const import (
    CONF_BONDED_SCANNER,
    CONF_HW_ID,
    CONF_PIN,
    CONF_PIN_BONDED,
    CONF_VERSION,
    DOMAIN,
)
from custom_components.pvvx_ota.ota import PairingRequiredError
from custom_components.pvvx_ota.shared import get_shared

from .fake_gatt import FakeClient
from .test_config import MAC, SAMPLE, patch_catalog
from .test_ha import FLOW_CONN, flow_conn, svc
from .translated import raises_key

PROXY = "AA:BB:CC:00:00:01"
OTHER = "AA:BB:CC:DD:EE:FF"


class Scanner:
    def __init__(self, source):
        self.source = source
        self.name = "proxy"


class Linked(FakeClient):
    """A fake client that says which proxy it is connected through."""

    def __init__(self, source=PROXY, **kw):
        super().__init__(**kw)
        self._connected_scanner = Scanner(source)


class Connects(list):
    """The clients establish_connection hands out, the paths the scanners offer, and what it was called with."""

    def __init__(self):
        super().__init__()
        self.devices = [path(PROXY, -64), path(OTHER, -50)]
        self.calls: list[tuple] = []


def path(source, rssi):
    return SimpleNamespace(
        scanner=Scanner(source), advertisement=SimpleNamespace(rssi=rssi), ble_device=SimpleNamespace(source=source)
    )


@pytest.fixture
def connects(monkeypatch):
    """async_connected with a stubbed Bluetooth stack: establish_connection hands out the next client."""
    clients = Connects()

    async def establish(client_cls, device, name, ble_device_callback=None):
        clients.calls.append((client_cls, device, ble_device_callback))
        return clients.pop(0)

    monkeypatch.setattr(dev.bluetooth, "async_ble_device_from_address", lambda *a, **k: object())
    monkeypatch.setattr(dev, "establish_connection", establish)
    monkeypatch.setattr(dev.bluetooth, "async_scanner_devices_by_address", lambda *a, **k: clients.devices)
    monkeypatch.setattr(dev, "ENCRYPT_SETTLE", 0.05)
    return clients


async def test_bonded_sensor_is_encrypted_by_pair_before_the_first_command(hass, connects, caplog):
    caplog.set_level(logging.DEBUG)
    client = Linked(needs_pairing=True)
    connects.append(client)
    async with dev.async_connected(hass, MAC, "ATC", encrypt=True, bonded_scanner=PROXY) as c:
        assert c.pair_calls == 1 and not c.needs_pairing  # nothing was sent before pair()
    assert client.disconnected
    assert "encrypted by pair()" in caplog.text


@pytest.mark.parametrize("kind", ["missing", "fails"])
async def test_bonded_sensor_waits_for_the_security_request_when_pair_is_unusable(hass, connects, caplog, kind):
    caplog.set_level(logging.DEBUG)
    client = Linked(pair_fails=True)
    if kind == "missing":
        client.pair = None  # a backend without pair()
    connects.append(client)
    async with dev.async_connected(hass, MAC, "ATC", encrypt=True, bonded_scanner=PROXY):
        pass
    assert "Security Request" in caplog.text and "first command sent" in caplog.text
    assert ("did not encrypt the link" in caplog.text) == (kind == "fails")  # a failed pair() is logged at DEBUG


async def test_bonded_sensor_connects_through_the_paired_proxys_path_only(hass, connects):
    client = Linked()
    connects.append(client)
    async with dev.async_connected(hass, MAC, "ATC", encrypt=True, bonded_scanner=PROXY):
        pass
    cls, device, refresh = connects.calls[0]
    assert device is connects.devices[0].ble_device  # the paired proxy's path, not the stronger other one
    assert issubclass(cls, dev.BleakClientWithServiceCache)
    assert refresh() is device
    connects.devices.clear()  # the paired proxy stops seeing it: the retry callback keeps the last device
    assert refresh() is device


async def test_paired_proxy_not_seeing_the_sensor_fails_before_connecting(hass, connects):
    connects.devices[:] = [path(OTHER, -50)]
    client = Linked(needs_pairing=True)
    connects.append(client)
    with raises_key("paired_proxy_does_not_see_sensor"):
        async with dev.async_connected(hass, MAC, "ATC", encrypt=True, bonded_scanner=PROXY):
            pytest.fail("must not get a client")
    assert connects.calls == [] and client.pair_calls == 0 and len(connects) == 1  # nothing was connected


async def test_scanner_data_that_fails_counts_as_not_seen(hass, connects, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no stack")

    monkeypatch.setattr(dev.bluetooth, "async_scanner_devices_by_address", boom)
    with raises_key("paired_proxy_does_not_see_sensor"):
        async with dev.async_connected(hass, MAC, "ATC", encrypt=True, bonded_scanner=PROXY):
            pass


async def test_connection_through_another_proxy_is_still_refused_without_sending(hass, connects):
    """Safety net: if habluetooth still routed it elsewhere, nothing is sent."""
    client = Linked(source=OTHER, needs_pairing=True)
    connects.append(client)
    with raises_key("connected_via_wrong_proxy", proxy=PROXY):
        async with dev.async_connected(hass, MAC, "ATC", encrypt=True, bonded_scanner=PROXY):
            pytest.fail("must not get a client")
    assert client.pair_calls == 0 and client.disconnected


async def test_pinned_client_class_only_uses_the_paired_scanner(monkeypatch):
    class Parent:
        def __init__(self, *a, **k):
            pass

        def _async_get_best_available_backend_and_device(self, manager):  # habluetooth's own choice
            raise AssertionError("not used")

        def _async_get_backend_for_ble_device(self, manager, scanner, ble_device):
            return None if ble_device.source == "full" else ("backend", ble_device.source)

    monkeypatch.setattr(bt, "BleakClientWithServiceCache", Parent)
    manager = SimpleNamespace(
        async_scanner_devices_by_address=lambda address, connectable: [
            path(OTHER, -40),
            path("full", -50),
            path(PROXY, -70),
        ]
    )
    cls = bt.pinned_client_class(PROXY, MAC)
    assert cls is not Parent
    assert cls()._async_get_best_available_backend_and_device(manager) == ("backend", PROXY)
    none = SimpleNamespace(async_scanner_devices_by_address=lambda address, connectable: [path(OTHER, -40)])
    with pytest.raises(bt.BleakError, match="paired proxy"):
        cls()._async_get_best_available_backend_and_device(none)
    full = SimpleNamespace(async_scanner_devices_by_address=lambda address, connectable: [path("full", -40)])
    cls2 = bt.pinned_client_class("full", MAC)
    with pytest.raises(bt.BleakError):
        cls2()._async_get_best_available_backend_and_device(full)


async def test_pinned_client_class_falls_back_when_habluetooth_has_no_such_method(monkeypatch):
    class Plain:
        pass

    monkeypatch.setattr(bt, "BleakClientWithServiceCache", Plain)
    monkeypatch.setattr(bt, "_warned", False)
    assert bt.pinned_client_class(PROXY, MAC) is None  # the caller connects normally


async def test_sensor_without_a_bond_is_untouched(hass, connects):
    client = Linked()
    connects.append(client)
    async with dev.async_connected(hass, MAC, "ATC") as c:  # no entry: not bonded
        assert c.pair_calls == 0


async def test_bond_is_looked_up_from_the_entry(hass, connects):
    assert dev.bond_for(hass, MAC) == (False, None, None)
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC,
        data={CONF_ADDRESS: MAC, CONF_PIN_BONDED: True, CONF_BONDED_SCANNER: PROXY},
    ).add_to_hass(hass)
    assert dev.bond_for(hass, MAC.lower()) == (True, PROXY, None)
    client = Linked(needs_pairing=True)
    connects.append(client)
    async with dev.async_connected(hass, MAC, "ATC") as c:
        assert c.pair_calls == 1
    assert dev.connected_source(object()) is None


def two_connections(first, second):
    """Flow helper: the next async_connected hands out these clients and records the keyword arguments."""
    clients, kwargs = [first, second], []

    @asynccontextmanager
    async def fake(hass, address, name, **kw):
        kwargs.append(kw)
        client = clients.pop(0)
        try:
            yield client
        finally:
            await client.disconnect()

    return patch(FLOW_CONN, fake), kwargs


async def test_flow_adds_a_pin_sensor_that_has_a_bond_in_the_serving_proxy(hass: HomeAssistant, enable_bluetooth):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    first = Linked(needs_pairing=True, pair_helps=False)  # plain connection: refused
    second = Linked(cfg=SAMPLE)  # encrypted through the same proxy: answers
    ctx, kwargs = two_connections(first, second)
    with ctx, patch("custom_components.pvvx_ota.async_setup_entry", return_value=True):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["data"][CONF_PIN_BONDED] is True and r["data"][CONF_BONDED_SCANNER] == PROXY
    assert kwargs == [{}, {"encrypt": True, "bonded_scanner": PROXY}]


async def test_flow_ignores_a_pin_sensor_nobody_has_a_bond_with(hass: HomeAssistant, enable_bluetooth):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    ctx, _ = two_connections(Linked(needs_pairing=True, pair_helps=False), Linked(needs_pairing=True, pair_helps=False))
    with ctx:
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "pin_protected"
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1 and entries[0].source == SOURCE_IGNORE


async def test_flow_second_connection_via_the_wrong_proxy_is_a_connect_error(hass: HomeAssistant, enable_bluetooth):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())

    @asynccontextmanager
    async def fake(hass_, address, name, **kw):
        if kw:
            raise HomeAssistantError("not reachable via the paired proxy")
        yield Linked(needs_pairing=True, pair_helps=False)

    with patch(FLOW_CONN, fake):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": "cannot_connect"}


async def test_lost_bond_raises_its_own_repair_issue(hass: HomeAssistant, enable_bluetooth):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC,
        title="ATC_000001",
        data={
            CONF_ADDRESS: MAC,
            CONF_VERSION: 0x59,
            CONF_HW_ID: 10,
            CONF_PIN_BONDED: True,
            CONF_BONDED_SCANNER: PROXY,
            CONF_PIN: 424242,  # a stored PIN whose re-pair did not help: the repair issue; without one: a reauth flow
        },
    )
    entry.add_to_hass(hass)

    @asynccontextmanager
    async def fake(hass_, address, name, **kw):
        yield FakeClient(cfg=SAMPLE, needs_pairing=True, pair_helps=False)

    p1, p2 = patch_catalog()
    ctx = (
        p1,
        p2,
        patch("custom_components.pvvx_ota.update.async_connected", fake),
        patch("custom_components.pvvx_ota.store.async_connected", fake),
    )
    for c in ctx:
        c.start()
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await entry.runtime_data.first_read
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        issues = ir.async_get(hass)
        assert issues.async_get_issue(DOMAIN, f"pin_bond_lost_{entry.entry_id}")
        assert issues.async_get_issue(DOMAIN, f"pin_set_{entry.entry_id}") is None
        # the next successful read clears it
        entry.runtime_data.set_raw(SAMPLE)
        assert issues.async_get_issue(DOMAIN, f"pin_bond_lost_{entry.entry_id}") is None
    finally:
        [c.stop() for c in ctx]


def test_link_description_does_not_repeat_the_mac(hass):
    from custom_components.pvvx_ota.device import describe_link

    for name, expected in [
        (
            "proxy-1 (AA:BB:CC:00:00:01)",
            "proxy-1 (AA:BB:CC:00:00:01), RSSI unknown",
        ),
        ("AA:BB:CC:00:00:01", "AA:BB:CC:00:00:01, RSSI unknown"),
        ("hci0", "hci0 (AA:BB:CC:00:00:01), RSSI unknown"),
    ]:
        client = SimpleNamespace(_connected_scanner=SimpleNamespace(source="AA:BB:CC:00:00:01", name=name))
        with patch.object(dev.bluetooth, "async_scanner_devices_by_address", return_value=[]):
            assert describe_link(hass, client, MAC) == expected


# -- recovery: a bonded link must answer; if not, pair again with the stored PIN ----------------------------


def stored(hass, pin):
    data = {CONF_ADDRESS: MAC, CONF_PIN_BONDED: True, CONF_BONDED_SCANNER: PROXY}
    if pin is not None:
        data[CONF_PIN] = pin
    MockConfigEntry(domain=DOMAIN, unique_id=MAC, data=data).add_to_hass(hass)


def silent():
    """pair() 'works' but the sensor never replies (the bond is not accepted)."""
    return Linked(no_reply=True, pair_helps=False)


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.setattr(dev, "PROBE_TIMEOUT", 0.05)
    monkeypatch.setattr("custom_components.pvvx_ota.ota.CFG_TIMEOUT", 0.05)


async def test_a_working_bond_is_verified_and_not_repaired(hass, connects, probe, monkeypatch):
    stored(hass, 424242)
    repair = AsyncMock(return_value=True)
    monkeypatch.setattr(dev, "async_repair", repair)
    connects.append(Linked(needs_pairing=True))
    async with dev.async_connected(hass, MAC, "ATC"):
        pass
    repair.assert_not_awaited() if hasattr(repair, "assert_not_awaited") else None
    assert len(connects.calls) == 1


@pytest.mark.parametrize("first", ["refused", "silent_twice"])
async def test_a_dead_bond_is_repaired_with_the_stored_pin_and_retried(hass, connects, probe, monkeypatch, first):
    stored(hass, 424242)
    repair = AsyncMock(return_value=True)
    monkeypatch.setattr(dev, "async_repair", repair)
    if first == "silent_twice":
        connects.append(silent())
        with pytest.raises(dev.SilentLinkError):  # the first silent probe changes nothing
            async with dev.async_connected(hass, MAC, "ATC"):
                pytest.fail("must not get a client")
        repair.assert_not_awaited()
    connects.append(Linked(needs_pairing=True, pair_helps=False) if first == "refused" else silent())
    connects.append(Linked())  # after pairing again the link answers
    async with dev.async_connected(hass, MAC, "ATC") as client:
        assert client is not None
    repair.assert_awaited_once_with(hass, MAC, PROXY, 424242)
    assert len(connects.calls) == (3 if first == "silent_twice" else 2)
    assert MAC not in get_shared(hass).silent_probes


async def test_one_silent_probe_keeps_the_bond_and_does_not_pair_again(hass, connects, probe, monkeypatch):
    stored(hass, 424242)
    repair = AsyncMock(return_value=True)
    monkeypatch.setattr(dev, "async_repair", repair)
    first = silent()
    connects.append(first)
    with pytest.raises(dev.SilentLinkError):
        async with dev.async_connected(hass, MAC, "ATC"):
            pytest.fail("must not get a client")
    repair.assert_not_awaited()
    assert first.disconnected and len(connects.calls) == 1
    assert get_shared(hass).silent_probes == {MAC: 1}


async def test_a_working_probe_resets_the_silent_count(hass, connects, probe, monkeypatch):
    stored(hass, 424242)
    repair = AsyncMock(return_value=True)
    monkeypatch.setattr(dev, "async_repair", repair)
    for client in (silent(), Linked(), silent()):
        connects.append(client)
        try:
            async with dev.async_connected(hass, MAC, "ATC"):
                pass
        except dev.SilentLinkError:
            pass
    repair.assert_not_awaited()  # never two silent probes in a row
    assert get_shared(hass).silent_probes == {MAC: 1}


async def test_an_authentication_error_pairs_again_at_once(hass, connects, probe, monkeypatch):
    stored(hass, 424242)
    repair = AsyncMock(return_value=True)
    monkeypatch.setattr(dev, "async_repair", repair)
    connects.extend([Linked(needs_pairing=True, pair_helps=False), Linked()])
    async with dev.async_connected(hass, MAC, "ATC"):
        pass
    repair.assert_awaited_once_with(hass, MAC, PROXY, 424242)


async def test_a_dead_bond_without_a_stored_pin_raises_without_repairing(hass, connects, probe, monkeypatch):
    stored(hass, None)
    repair = AsyncMock(return_value=True)
    monkeypatch.setattr(dev, "async_repair", repair)
    first = Linked(needs_pairing=True, pair_helps=False)
    connects.append(first)
    with pytest.raises(PairingRequiredError, match="no PIN stored"):
        async with dev.async_connected(hass, MAC, "ATC"):
            pytest.fail("must not get a client")
    repair.assert_not_awaited()
    assert first.disconnected


async def test_repair_that_cannot_run_or_does_not_help_raises(hass, connects, probe, monkeypatch):
    stored(hass, 424242)
    for helps in (False, True):
        monkeypatch.setattr(dev, "async_repair", AsyncMock(return_value=helps))
        get_shared(hass).silent_probes[MAC] = 1  # the second silent probe in a row
        connects[:] = [silent()] + ([silent()] if helps else [])
        with pytest.raises(PairingRequiredError):
            async with dev.async_connected(hass, MAC, "ATC"):
                pytest.fail("must not get a client")
        assert all(c.disconnected for c in [])  # nothing left open (clients are consumed from the list)


async def test_repair_proxy_lookup(hass, monkeypatch):
    from custom_components.pvvx_ota import pairing

    proxy = pairing.PairingProxy(PROXY, "proxy-1", "proxy_1_pair_pvvx")
    monkeypatch.setattr(pairing, "pairing_proxies", lambda hass_, address: [proxy])
    pair = AsyncMock()
    monkeypatch.setattr(pairing, "async_pair", pair)
    assert await pairing.async_repair(hass, MAC, PROXY.lower(), 424242) is True
    pair.assert_awaited_once_with(hass, proxy, MAC, 424242)
    assert await pairing.async_repair(hass, MAC, "AA:AA:AA:AA:AA:AA", 424242) is False  # that proxy has no such action
    pair.side_effect = HomeAssistantError("service gone")
    assert await pairing.async_repair(hass, MAC, PROXY, 424242) is False


def test_expected_path_is_the_paired_proxy_for_a_pin_sensor_else_the_strongest(hass, monkeypatch):
    far, near = path("11:11:11:11:11:11", -80), path(PROXY, -70)
    near.scanner.name = "proxy-1 (AA:BB:CC:00:00:01)"
    stronger = path("22:22:22:22:22:22", -50)
    stronger.scanner.name = "proxy-2 (22:22:22:22:22:22)"
    monkeypatch.setattr(dev.bluetooth, "async_scanner_devices_by_address", lambda *a, **k: [far, near, stronger])
    assert dev.expected_path(hass, MAC) == ("proxy-2", -50)  # no entry: the strongest
    stored(hass, 424242)
    assert dev.expected_path(hass, MAC) == ("proxy-1", -70)  # the paired proxy, not the stronger one
    monkeypatch.setattr(dev.bluetooth, "async_scanner_devices_by_address", lambda *a, **k: [far])
    assert dev.expected_path(hass, MAC) is None  # the paired proxy does not hear it
    monkeypatch.setattr(dev.bluetooth, "async_scanner_devices_by_address", lambda *a, **k: 1 / 0)
    assert dev.expected_path(hass, MAC) is None


async def test_missing_hook_is_logged_once_and_the_connection_is_checked_afterwards(monkeypatch, caplog):
    class Plain:
        pass

    monkeypatch.setattr(bt, "BleakClientWithServiceCache", Plain)
    monkeypatch.setattr(bt, "_warned", False)
    caplog.set_level("WARNING")
    for _ in range(3):
        assert bt.pinned_client_class(PROXY, MAC) is None
    assert [r.message for r in caplog.records].count(caplog.records[0].message) == 1 and len(caplog.records) == 1
    assert "no longer has the Bluetooth hook" in caplog.text


async def test_pinned_sensor_is_refused_when_the_connection_path_cannot_be_verified(hass, connects, monkeypatch):
    """No hook to pin the proxy and no way to see which one served the link: nothing is sent to a PIN sensor."""
    monkeypatch.setattr(bt, "pinned_client_class", lambda source, address: None)
    client = FakeClient()  # no _connected_scanner at all
    connects.append(client)
    with raises_key("proxy_not_verifiable"):
        async with dev.async_connected(hass, MAC, "ATC", encrypt=True, bonded_scanner=PROXY):
            pytest.fail("must not get a client")
    assert client.pair_calls == 0 and client.writes == [] and client.disconnected
    assert dev.connected_source(client) is None and bt.connected_scanner(client) is None


async def test_unpinned_connection_still_works_for_a_sensor_without_a_bond(hass, connects, monkeypatch):
    monkeypatch.setattr(bt, "pinned_client_class", lambda source, address: None)
    client = FakeClient()
    connects.append(client)
    async with dev.async_connected(hass, MAC, "ATC") as c:
        assert c is client


# -- S5: the paired proxy is gone ----------------------------------------------------------------------------


def proxy_gone():
    return dev.ha_error("paired_proxy_does_not_see_sensor", name="ATC", proxy=PROXY)


async def test_three_failed_reads_because_the_proxy_is_gone_raise_a_repair_issue(hass, enable_bluetooth):
    from .test_pin_pairing import issue, pin_store

    entry, _client, ctx = await pin_store(hass, bonded=True)
    try:
        store = entry.runtime_data

        @asynccontextmanager
        async def gone(*a, **k):
            raise proxy_gone()
            yield

        with patch("custom_components.pvvx_ota.store.async_connected", gone):
            for _ in range(2):
                with pytest.raises(HomeAssistantError):
                    await store.async_refresh()
            assert issue(hass, entry, "pin_proxy_missing") is None
            with pytest.raises(HomeAssistantError):
                await store.async_refresh()
        found = issue(hass, entry, "pin_proxy_missing")
        assert found and found.translation_placeholders["proxy"] == PROXY and found.translation_placeholders["name"]
        assert not found.is_fixable
        store.set_raw(SAMPLE)  # the next successful read
        assert issue(hass, entry, "pin_proxy_missing") is None
    finally:
        [c.stop() for c in ctx]


async def test_another_failure_restarts_the_proxy_missing_count(hass, enable_bluetooth):
    from .test_pin_pairing import issue, pin_store

    entry, _client, ctx = await pin_store(hass, bonded=True)
    try:
        store = entry.runtime_data
        for err in (proxy_gone(), proxy_gone(), HomeAssistantError("other"), proxy_gone(), proxy_gone()):
            store.mark_unreachable(err)
        assert issue(hass, entry, "pin_proxy_missing") is None
        store.mark_unreachable(proxy_gone())
        assert issue(hass, entry, "pin_proxy_missing") is not None
    finally:
        [c.stop() for c in ctx]


async def test_the_first_read_counts_every_failed_attempt(hass, enable_bluetooth, monkeypatch):
    from .test_pin_pairing import issue, pin_store

    entry, _client, ctx = await pin_store(hass, bonded=True)
    try:
        store = entry.runtime_data
        store.raw = None
        monkeypatch.setattr("custom_components.pvvx_ota.store.FIRST_READ_RETRY", (0, 0))
        monkeypatch.setattr(type(store), "needs_read", lambda self: True)
        with patch.object(type(store), "async_initial_read", AsyncMock(side_effect=proxy_gone())):
            await store.async_first_read()
        assert issue(hass, entry, "pin_proxy_missing") is not None
    finally:
        [c.stop() for c in ctx]


# -- S2: the flow does not wait for the firmware lock ---------------------------------------------------------


async def test_flow_answers_at_once_while_a_firmware_operation_holds_the_lock(hass: HomeAssistant, enable_bluetooth):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    lock = get_shared(hass).lock
    async with lock:
        with patch(FLOW_CONN, side_effect=AssertionError("must not connect")):
            r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
        assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": "firmware_busy"}
    with flow_conn(FakeClient()), patch("custom_components.pvvx_ota.async_setup_entry", return_value=True):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.CREATE_ENTRY


async def test_pairing_does_not_wait_for_the_firmware_lock_either(hass: HomeAssistant, enable_bluetooth):
    from custom_components.pvvx_ota.config_flow import PvvxOtaConfigFlow

    flow = PvvxOtaConfigFlow()
    flow.hass = hass
    offer = SimpleNamespace(source=PROXY, name="proxy")
    with patch("custom_components.pvvx_ota.config_flow.pairing_proxies", return_value=[offer]):
        async with get_shared(hass).lock:
            outcome = await flow._async_pair_any(MAC, "ATC", 123456)
    assert outcome.error == "firmware_busy" and outcome.ok is None


async def test_a_silent_probe_is_a_plain_unreachable_read_not_a_lost_bond(hass, enable_bluetooth, monkeypatch):
    from .test_pin_pairing import issue, pin_store

    entry, _client, ctx = await pin_store(hass, bonded=True)
    try:
        store = entry.runtime_data
        err = dev.SilentLinkError("no reply")
        with patch.object(type(store), "async_initial_read", AsyncMock(side_effect=err)):
            monkeypatch.setattr("custom_components.pvvx_ota.store.FIRST_READ_RETRY", (0,))
            monkeypatch.setattr(type(store), "needs_read", lambda self: True)
            store.raw = None
            await store.async_first_read()  # retried, then given up quietly
        assert store._unreachable
        store.pairing_lost(err)
        assert issue(hass, entry, "pin_bond_lost") is None and issue(hass, entry, "pin_set") is None
    finally:
        [c.stop() for c in ctx]
