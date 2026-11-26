"""Pairing a PIN sensor through a proxy that offers the pair_pvvx action: proxy lookup, flow PIN step, re-pair, redaction."""

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_BLUETOOTH
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pvvx_ota import pairing
from custom_components.pvvx_ota.const import (
    CONF_BONDED_SCANNER,
    CONF_HW_ID,
    CONF_PIN,
    CONF_PIN_BONDED,
    CONF_VERSION,
    DOMAIN,
)
from custom_components.pvvx_ota.diagnostics import async_get_config_entry_diagnostics
from custom_components.pvvx_ota.ota import PairingRequiredError

from .test_config import MAC, SAMPLE, patch_catalog
from .test_ha import FLOW_CONN, fast_read_cfg, svc
from .test_pin_bond import PROXY, Linked, connects, probe  # noqa: F401
from .translated import raises_key

SECRET_PIN = 424242


async def submit(hass, flow_id, pin):
    """Submit the PIN form and follow the progress step to the flow's next result."""
    r = await hass.config_entries.flow.async_configure(flow_id, {CONF_PIN: pin})
    while r["type"] is FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        r = await hass.config_entries.flow.async_configure(flow_id)
    return r


def scanner_device(name, source, rssi):
    return SimpleNamespace(scanner=SimpleNamespace(name=name, source=source), advertisement=SimpleNamespace(rssi=rssi))


def offer(hass, node):
    calls: list[ServiceCall] = []

    async def handler(call: ServiceCall):
        calls.append(call)

    hass.services.async_register("esphome", f"{node}_pair_pvvx", handler)
    return calls


async def test_proxies_offering_the_action_strongest_first(hass: HomeAssistant):
    offer(hass, "proxy_1")
    offer(hass, "proxy_2")
    devices = [
        scanner_device("proxy-2", "B", -81),
        scanner_device("proxy-1", PROXY, -64),
        scanner_device("proxy-3", "W", -50),  # strongest, but no action
        scanner_device("", "X", -40),  # nameless scanner
    ]
    with patch.object(pairing.bluetooth, "async_scanner_devices_by_address", return_value=devices):
        found = pairing.pairing_proxies(hass, MAC)
    assert [(p.name, p.source) for p in found] == [("proxy-1", PROXY), ("proxy-2", "B")]
    assert found[0].service == "proxy_1_pair_pvvx"
    with patch.object(pairing.bluetooth, "async_scanner_devices_by_address", side_effect=RuntimeError("no stack")):
        assert pairing.pairing_proxies(hass, MAC) == []


async def test_pair_calls_the_action_and_never_logs_the_pin(hass: HomeAssistant, caplog):
    caplog.set_level(logging.DEBUG)
    calls = offer(hass, "proxy_1")
    proxy = pairing.PairingProxy(PROXY, "proxy-1", "proxy_1_pair_pvvx")
    await pairing.async_pair(hass, proxy, MAC, SECRET_PIN, wait=0)
    assert len(calls) == 1 and calls[0].data == {"mac": MAC, "pin": SECRET_PIN}
    assert str(SECRET_PIN) not in caplog.text
    gone = pairing.PairingProxy(PROXY, "x", "no_such_pair_pvvx")
    try:
        await pairing.async_pair(hass, gone, MAC, SECRET_PIN, wait=0)
    except HomeAssistantError as err:
        assert str(SECRET_PIN) not in str(err)
    else:
        raise AssertionError("an unknown service must fail")


def connections(*clients):
    seq, kwargs = list(clients), []

    @asynccontextmanager
    async def fake(hass, address, name, **kw):
        kwargs.append(kw)
        client = seq.pop(0)
        try:
            yield client
        finally:
            await client.disconnect()

    return patch(FLOW_CONN, fake), kwargs


def refused():
    return Linked(needs_pairing=True, pair_helps=False)


PROXY_OFFER = pairing.PairingProxy(PROXY, "proxy-1", "proxy_1_pair_pvvx")


async def test_flow_asks_for_the_pin_pairs_and_stores_it(hass: HomeAssistant, enable_bluetooth, caplog):
    caplog.set_level(logging.DEBUG)
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    ctx, kwargs = connections(refused(), Linked(cfg=SAMPLE))
    pair = AsyncMock()
    with (
        ctx,
        patch("custom_components.pvvx_ota.config_flow.pairing_proxies", return_value=[PROXY_OFFER]),
        patch("custom_components.pvvx_ota.config_flow.async_pair", pair),
        patch("custom_components.pvvx_ota.async_setup_entry", return_value=True),
    ):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
        assert r["type"] is FlowResultType.FORM and r["step_id"] == "pin"
        r = await submit(hass, r["flow_id"], SECRET_PIN)
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["data"][CONF_PIN] == SECRET_PIN and r["data"][CONF_PIN_BONDED] is True
    assert r["data"][CONF_BONDED_SCANNER] == PROXY
    pair.assert_awaited_once_with(hass, PROXY_OFFER, MAC, SECRET_PIN)
    assert kwargs[-1] == {"encrypt": True, "bonded_scanner": PROXY}
    assert str(SECRET_PIN) not in caplog.text


async def test_flow_wrong_pin_can_be_retried(hass: HomeAssistant, enable_bluetooth):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    ctx, _ = connections(refused(), refused(), Linked(cfg=SAMPLE))
    with (
        ctx,
        patch("custom_components.pvvx_ota.config_flow.pairing_proxies", return_value=[PROXY_OFFER]),
        patch("custom_components.pvvx_ota.config_flow.async_pair", AsyncMock()),
        patch("custom_components.pvvx_ota.async_setup_entry", return_value=True),
    ):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
        r = await submit(hass, r["flow_id"], 111111)
        assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": "pin_wrong"}
        r = await submit(hass, r["flow_id"], SECRET_PIN)
    assert r["type"] is FlowResultType.CREATE_ENTRY


async def test_flow_pin_step_errors_when_the_proxy_is_gone_or_unreachable(hass: HomeAssistant, enable_bluetooth):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    ctx, _ = connections(refused())
    offers = [[PROXY_OFFER], [], [PROXY_OFFER]]
    with (
        ctx,
        patch("custom_components.pvvx_ota.config_flow.pairing_proxies", side_effect=lambda *a: offers.pop(0)),
        patch("custom_components.pvvx_ota.config_flow.async_pair", AsyncMock()),
    ):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
        assert r["step_id"] == "pin"
        r = await submit(hass, r["flow_id"], SECRET_PIN)
        assert r["errors"] == {"base": "no_pairing_proxy"}
    # the pairing call itself fails
    with (
        patch("custom_components.pvvx_ota.config_flow.pairing_proxies", return_value=[PROXY_OFFER]),
        patch("custom_components.pvvx_ota.config_flow.async_pair", AsyncMock(side_effect=HomeAssistantError("x"))),
    ):
        r = await submit(hass, r["flow_id"], SECRET_PIN)
    assert r["errors"] == {"base": "cannot_connect"}


def bonded_entry(hass, pin=SECRET_PIN):
    data = {CONF_ADDRESS: MAC, CONF_VERSION: 0x59, CONF_HW_ID: 10, CONF_PIN_BONDED: True, CONF_BONDED_SCANNER: PROXY}
    if pin is not None:
        data[CONF_PIN] = pin
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, title="ATC_000001", data=data)
    entry.add_to_hass(hass)
    return entry


async def setup_with(hass, entry, clients):
    seq = list(clients)

    @asynccontextmanager
    async def fake(hass_, address, name, **kw):
        yield seq.pop(0) if len(seq) > 1 else seq[0]

    p1, p2 = patch_catalog()
    ctx = (
        p1,
        p2,
        patch("custom_components.pvvx_ota.update.async_connected", fake),
        patch("custom_components.pvvx_ota.store.async_connected", fake),
    )
    for c in ctx:
        c.start()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await entry.runtime_data.first_read
    await hass.async_block_till_done()
    return ctx


async def test_diagnostics_redact_the_stored_pin(hass: HomeAssistant, enable_bluetooth):
    entry = bonded_entry(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["data"][CONF_PIN] == "**REDACTED**"
    assert str(SECRET_PIN) not in str(diag)


async def test_flow_without_a_pairing_proxy_treats_a_silent_sensor_after_pair_as_no_bond(
    hass: HomeAssistant, enable_bluetooth
):
    """Bond probing (hand-written ble_client route): without a bond pair() stalls and the sensor stays silent
    (NotPvvxError): that is "no bond" (ignored, pin_protected), not a "not pvvx" abort."""
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    ctx, _ = connections(refused(), Linked(no_reply=True))
    with (
        ctx,
        patch("custom_components.pvvx_ota.config_flow.pairing_proxies", return_value=[]),
        patch("custom_components.pvvx_ota.ota.CFG_TIMEOUT", 0.05),
        patch("custom_components.pvvx_ota.config_flow.read_cfg", fast_read_cfg),
    ):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "pin_protected"


async def test_flow_with_a_pairing_proxy_goes_straight_to_the_pin_step(hass: HomeAssistant, enable_bluetooth):
    """No bond probing when a proxy can pair: a probe on a bond-less proxy leaves its security manager busy."""
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    ctx, kwargs = connections(refused())  # a second connection would run out of clients
    with ctx, patch("custom_components.pvvx_ota.config_flow.pairing_proxies", return_value=[PROXY_OFFER]):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "pin" and kwargs == [{}]


def test_node_name_drops_the_mac_habluetooth_appends():
    assert pairing.node_name("proxy-3 (AA:BB:CC:00:00:03)") == "proxy-3"
    assert pairing.node_name("proxy-3") == "proxy-3"
    assert pairing.node_name("proxy (not a mac)") == "proxy (not a mac)"


async def test_proxies_are_found_by_their_real_scanner_names(hass: HomeAssistant):
    """habluetooth names an ESPHome proxy '<node> (<MAC>)': the service belongs to the node alone."""
    offer(hass, "proxy_3")
    devices = [scanner_device("proxy-3 (AA:BB:CC:00:00:03)", "AA:BB:CC:00:00:03", -78)]
    with patch.object(pairing.bluetooth, "async_scanner_devices_by_address", return_value=devices):
        found = pairing.pairing_proxies(hass, MAC)
    assert [(p.name, p.source, p.service) for p in found] == [("proxy-3", "AA:BB:CC:00:00:03", "proxy_3_pair_pvvx")]


# -- reauthentication and reconfiguration ----------------------------------------------------------------------


def pin_entry(hass, pin=None, bonded=True):
    data = {CONF_ADDRESS: MAC, CONF_VERSION: 0x59, CONF_HW_ID: 10}
    if bonded:
        data |= {CONF_PIN_BONDED: True, CONF_BONDED_SCANNER: "OLD:PROXY"}
    if pin is not None:
        data[CONF_PIN] = pin
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, title="ATC Sensor 3", data=data)
    entry.add_to_hass(hass)
    return entry


async def test_reauth_asks_for_the_pin_pairs_and_updates_the_entry(hass: HomeAssistant, enable_bluetooth, caplog):
    caplog.set_level(logging.DEBUG)
    entry = pin_entry(hass)
    r = await entry.start_reauth_flow(hass)
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "reauth_confirm"
    assert r["description_placeholders"]["name"] == "ATC Sensor 3"
    ctx, kwargs = connections(Linked(cfg=SAMPLE))
    pair = AsyncMock()
    with (
        ctx,
        patch("custom_components.pvvx_ota.config_flow.pairing_proxies", return_value=[PROXY_OFFER]),
        patch("custom_components.pvvx_ota.config_flow.async_pair", pair),
        patch("custom_components.pvvx_ota.async_setup_entry", return_value=True),
    ):
        r = await submit(hass, r["flow_id"], SECRET_PIN)
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "reauth_successful"
    assert entry.data[CONF_PIN] == SECRET_PIN and entry.data[CONF_BONDED_SCANNER] == PROXY
    assert entry.data[CONF_PIN_BONDED] is True
    pair.assert_awaited_once_with(hass, PROXY_OFFER, MAC, SECRET_PIN)
    assert kwargs == [{"encrypt": True, "bonded_scanner": PROXY}]
    assert str(SECRET_PIN) not in caplog.text


async def test_reauth_wrong_pin_or_no_pairing_proxy_shows_errors_and_can_retry(hass: HomeAssistant, enable_bluetooth):
    entry = pin_entry(hass)
    r = await entry.start_reauth_flow(hass)
    with patch("custom_components.pvvx_ota.config_flow.pairing_proxies", return_value=[]):
        r = await submit(hass, r["flow_id"], 111111)
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": "no_pairing_proxy"}
    ctx, _ = connections(refused(), Linked(cfg=SAMPLE))
    with (
        ctx,
        patch("custom_components.pvvx_ota.config_flow.pairing_proxies", return_value=[PROXY_OFFER]),
        patch("custom_components.pvvx_ota.config_flow.async_pair", AsyncMock()),
        patch("custom_components.pvvx_ota.async_setup_entry", return_value=True),
    ):
        r = await submit(hass, r["flow_id"], 111111)
        assert r["errors"] == {"base": "pin_wrong"}  # the sensor still refuses
        r = await submit(hass, r["flow_id"], SECRET_PIN)
    assert r["type"] is FlowResultType.ABORT and entry.data[CONF_PIN] == SECRET_PIN


async def test_reauth_not_pvvx_after_pairing_aborts(hass: HomeAssistant, enable_bluetooth):
    entry = pin_entry(hass)
    r = await entry.start_reauth_flow(hass)
    with (
        patch("custom_components.pvvx_ota.config_flow.pairing_proxies", return_value=[PROXY_OFFER]),
        patch("custom_components.pvvx_ota.config_flow.async_pair", AsyncMock()),
        patch("custom_components.pvvx_ota.config_flow.read_cfg", AsyncMock(return_value=(None, None))),
        patch(FLOW_CONN, connections(Linked(cfg=SAMPLE))[0].new),
    ):
        r = await submit(hass, r["flow_id"], SECRET_PIN)
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "not_pvvx"


async def test_reconfigure_stores_the_pin_of_a_bonded_sensor_only(hass: HomeAssistant, enable_bluetooth):
    plain = pin_entry(hass, bonded=False)
    r = await plain.start_reconfigure_flow(hass)
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "no_pin_sensor"
    await hass.config_entries.async_remove(plain.entry_id)

    entry = pin_entry(hass)
    r = await entry.start_reconfigure_flow(hass)
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "reconfigure"
    ctx, _ = connections(Linked(cfg=SAMPLE))
    with (
        ctx,
        patch("custom_components.pvvx_ota.config_flow.pairing_proxies", return_value=[PROXY_OFFER]),
        patch("custom_components.pvvx_ota.config_flow.async_pair", AsyncMock()),
        patch("custom_components.pvvx_ota.async_setup_entry", return_value=True),
    ):
        r = await submit(hass, r["flow_id"], SECRET_PIN)
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PIN] == SECRET_PIN


async def test_dead_bond_without_a_pin_starts_reauth_but_unreachable_does_not(
    hass: HomeAssistant, connects, monkeypatch
):
    from custom_components.pvvx_ota import device as dev

    monkeypatch.setattr(dev, "PROBE_TIMEOUT", 0.05)
    monkeypatch.setattr("custom_components.pvvx_ota.ota.CFG_TIMEOUT", 0.05)
    entry = pin_entry(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_BONDED_SCANNER: PROXY})
    started = []
    monkeypatch.setattr(type(entry), "async_start_reauth", lambda self, hass_, *a, **k: started.append(self.entry_id))
    # unreachable: the paired proxy does not hear the sensor, no connection, no reauth
    connects.devices.clear()
    with raises_key("paired_proxy_does_not_see_sensor"):
        async with dev.async_connected(hass, MAC, "ATC"):
            pass
    assert started == []
    # a dead bond (the sensor refuses after the encrypt step) and no PIN: reauth
    connects.devices[:] = [
        SimpleNamespace(
            scanner=SimpleNamespace(source=PROXY, name="p"),
            advertisement=SimpleNamespace(rssi=-50),
            ble_device=object(),
        )
    ]
    connects.append(refused())
    with pytest.raises(PairingRequiredError):
        async with dev.async_connected(hass, MAC, "ATC"):
            pass
    assert started == [entry.entry_id]
    # with a stored PIN and a successful re-pair: no reauth
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_PIN: SECRET_PIN})
    monkeypatch.setattr(dev, "async_repair", AsyncMock(return_value=True))
    connects.extend([refused(), Linked(cfg=SAMPLE)])
    async with dev.async_connected(hass, MAC, "ATC"):
        pass
    assert started == [entry.entry_id]
    # the config flow's own verification (explicit encrypt) never starts a reauth
    hass.config_entries.async_update_entry(entry, data={k: v for k, v in entry.data.items() if k != CONF_PIN})
    connects.append(refused())
    with pytest.raises(PairingRequiredError):
        async with dev.async_connected(hass, MAC, "ATC", encrypt=True, bonded_scanner=PROXY):
            pass
    assert started == [entry.entry_id]


# -- waiting for the proxy's pairer to be done ---------------------------------------------------------------

NODE = "proxy-1"


def esphome_node(hass, node=NODE):
    entry = MockConfigEntry(domain="esphome", title=node, data={"device_name": node})
    entry.add_to_hass(hass)
    return entry


def node_entity(hass, entry, domain, name, uid):
    return (
        er.async_get(hass).async_get_or_create(domain, "esphome", uid, config_entry=entry, original_name=name).entity_id
    )


def proxy_with_busy_entity(hass, node=NODE, name="pvvx pairer busy"):
    return node_entity(hass, esphome_node(hass, node), "binary_sensor", name, f"{node}-busy")


async def test_busy_and_result_entities_are_found_through_the_nodes_config_entry(hass: HomeAssistant):
    assert pairing.busy_entity(hass, NODE) is None  # no such node
    entry = esphome_node(hass)
    busy = node_entity(hass, entry, "binary_sensor", "pvvx pairer busy", "busy")
    result = node_entity(hass, entry, "sensor", "pvvx pair result", "result")
    node_entity(hass, entry, "binary_sensor", "Fan", "fan")
    assert pairing.busy_entity(hass, NODE) == busy and pairing.result_entity(hass, NODE) == result
    assert pairing.busy_entity(hass, "proxy_1") == busy  # slug-equal names match
    assert pairing.busy_entity(hass, "proxy-2") is None
    other = esphome_node(hass, "proxy-2")
    node_entity(hass, other, "binary_sensor", "Fan", "fan2")
    assert pairing.busy_entity(hass, "proxy-2") is None  # a node whose package has no such entity


async def test_pair_waits_for_the_pairer_to_go_idle(hass: HomeAssistant, monkeypatch):
    import asyncio

    monkeypatch.setattr(pairing, "PAIR_BUSY_APPEAR", 0.05)
    monkeypatch.setattr(pairing, "PAIR_AFTER_IDLE", 0.05)
    entity_id = proxy_with_busy_entity(hass)
    hass.states.async_set(entity_id, "off")
    calls = offer(hass, "proxy_1")
    order: list[str] = []

    async def pairer():
        hass.states.async_set(entity_id, "on")
        await asyncio.sleep(0.3)
        order.append("pairer done")
        hass.states.async_set(entity_id, "off")

    task = hass.async_create_task(pairer())
    await pairing.async_pair(hass, PROXY_OFFER, MAC, SECRET_PIN, wait=0)
    order.append("pair returned")
    await task
    assert order == ["pairer done", "pair returned"] and len(calls) == 1  # it did not return while the pairer was busy


async def test_pair_gives_up_waiting_for_a_stuck_pairer_and_falls_back_without_the_entity(
    hass: HomeAssistant, monkeypatch, caplog
):
    monkeypatch.setattr(pairing, "PAIR_BUSY_APPEAR", 0.01)
    monkeypatch.setattr(pairing, "PAIR_AFTER_IDLE", 0.01)
    monkeypatch.setattr(pairing, "PAIR_BUSY_TIMEOUT", 0.1)
    entity_id = proxy_with_busy_entity(hass)
    hass.states.async_set(entity_id, "on")  # never goes off
    offer(hass, "proxy_1")
    await pairing.async_pair(hass, PROXY_OFFER, MAC, SECRET_PIN, wait=0)
    assert "still busy" in caplog.text
    # an older package without the entity: the fixed wait
    caplog.clear()
    older = pairing.PairingProxy("11:22:33:44:55:66", "old", "old_pair_pvvx")
    offer(hass, "old")
    await pairing.async_pair(hass, older, MAC, SECRET_PIN, wait=0)
    assert "no pairer-busy entity" in caplog.text


async def test_pairer_that_never_reached_the_sensor_is_retried_then_reported(hass: HomeAssistant, monkeypatch):
    monkeypatch.setattr(pairing, "PAIR_BUSY_APPEAR", 0.01)
    monkeypatch.setattr(pairing, "PAIR_AFTER_IDLE", 0.01)
    entry = esphome_node(hass)
    busy = node_entity(hass, entry, "binary_sensor", "pvvx pairer busy", "busy")
    result = node_entity(hass, entry, "sensor", "pvvx pair result", "result")
    hass.states.async_set(busy, "off")
    calls = offer(hass, "proxy_1")
    hass.states.async_set(result, "no connection")
    with raises_key("pair_connect_failed", pairing.PairConnectError):
        await pairing.async_pair(hass, PROXY_OFFER, MAC, SECRET_PIN, wait=0)
    assert len(calls) == 2  # tried twice
    # the second attempt reaches the sensor
    calls.clear()
    states = iter(["no connection", "connected"])

    async def handler(call):
        calls.append(call)
        hass.states.async_set(result, next(states))

    hass.services.async_register("esphome", "proxy_1_pair_pvvx", handler)
    await pairing.async_pair(hass, PROXY_OFFER, MAC, SECRET_PIN, wait=0)
    assert len(calls) == 2


async def test_flow_reports_a_pairer_that_could_not_connect(hass: HomeAssistant, enable_bluetooth):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    ctx, _ = connections(refused())
    with (
        ctx,
        patch("custom_components.pvvx_ota.config_flow.pairing_proxies", return_value=[PROXY_OFFER]),
        patch(
            "custom_components.pvvx_ota.config_flow.async_pair",
            AsyncMock(side_effect=pairing.PairConnectError("proxy-1", MAC)),
        ),
    ):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
        r = await submit(hass, r["flow_id"], SECRET_PIN)
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": "pair_connect_failed"}


def test_link_check_waits_as_long_as_a_normal_config_read():
    """A sensor with a slow connection interval needs about 2 s per GATT operation: 4 s misjudged a healthy link."""
    from custom_components.pvvx_ota import device as dev
    from custom_components.pvvx_ota.ota import CFG_TIMEOUT

    assert dev.PROBE_TIMEOUT >= CFG_TIMEOUT


async def test_pairing_without_a_passkey_prompt_is_named_in_the_flow(hass: HomeAssistant, enable_bluetooth):
    """The proxy paired but the sensor never asked for the PIN (Just Works) and then refuses the key: not a wrong PIN."""
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    ctx, _ = connections(refused(), refused(), Linked(cfg=SAMPLE))
    results = iter(["no passkey asked", "passkey answered"])
    with (
        ctx,
        patch("custom_components.pvvx_ota.config_flow.pairing_proxies", return_value=[PROXY_OFFER]),
        patch("custom_components.pvvx_ota.config_flow.async_pair", AsyncMock(side_effect=lambda *a: next(results))),
        patch("custom_components.pvvx_ota.async_setup_entry", return_value=True),
    ):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
        r = await submit(hass, r["flow_id"], SECRET_PIN)
        assert r["errors"] == {"base": "pair_no_passkey"}
        r = await submit(hass, r["flow_id"], SECRET_PIN)
    assert r["type"] is FlowResultType.CREATE_ENTRY


async def test_pair_returns_what_the_proxy_reports(hass: HomeAssistant, monkeypatch):
    monkeypatch.setattr(pairing, "PAIR_BUSY_APPEAR", 0.01)
    monkeypatch.setattr(pairing, "PAIR_AFTER_IDLE", 0.01)
    entry = esphome_node(hass)
    busy = node_entity(hass, entry, "binary_sensor", "pvvx pairer busy", "busy")
    result = node_entity(hass, entry, "sensor", "pvvx pair result", "result")
    hass.states.async_set(busy, "off")
    offer(hass, "proxy_1")
    assert await pairing.async_pair(hass, PROXY_OFFER, MAC, SECRET_PIN, wait=0) is None  # no result reported yet
    hass.states.async_set(result, "no passkey asked")
    assert await pairing.async_pair(hass, PROXY_OFFER, MAC, SECRET_PIN, wait=0) == "no passkey asked"
    older = pairing.PairingProxy("11:22:33:44:55:66", "old", "old_pair_pvvx")
    offer(hass, "old")
    assert await pairing.async_pair(hass, older, MAC, SECRET_PIN, wait=0) is None  # an older package: unknown


# -- firmware info in the failure message, and the repair issue a failed pairing leaves ---------------------------


async def test_second_failed_pairing_leaves_a_repair_issue_that_goes_when_the_sensor_is_added(
    hass: HomeAssistant, enable_bluetooth
):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    bad = lambda: Linked(needs_pairing=True, pair_helps=False)  # noqa: E731
    ctx, _ = connections(refused(), bad(), bad(), Linked(cfg=SAMPLE))
    issues = ir.async_get(hass)
    with (
        ctx,
        patch("custom_components.pvvx_ota.config_flow.pairing_proxies", return_value=[PROXY_OFFER]),
        patch("custom_components.pvvx_ota.config_flow.async_pair", AsyncMock(return_value="passkey answered")),
        patch("custom_components.pvvx_ota.async_setup_entry", return_value=True),
    ):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
        r = await submit(hass, r["flow_id"], 111111)
        assert r["errors"] == {"base": "pin_wrong"}
        assert issues.async_get_issue(DOMAIN, f"pin_pairing_failed_{MAC}") is None  # one failure: only the form's error
        r = await submit(hass, r["flow_id"], 111111)
        issue = issues.async_get_issue(DOMAIN, f"pin_pairing_failed_{MAC}")  # the second one leaves a trace
        assert issue is not None and (
            issue.translation_placeholders["name"],
            issue.translation_placeholders["address"],
        ) == ("ATC_000001 (00:00:01)", MAC)
        r = await submit(hass, r["flow_id"], SECRET_PIN)
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert issues.async_get_issue(DOMAIN, f"pin_pairing_failed_{MAC}") is None  # gone once the sensor is added


# -- a sensor that still refuses after a correct pairing is not retried through other proxies ---------------


def proxies(n):
    return [pairing.PairingProxy(f"AA:00:00:00:00:0{i}", f"proxy-{i}", f"proxy_{i}_pair_pvvx") for i in range(n)]


async def test_only_the_best_proxy_pairs_and_a_refusing_sensor_gets_the_checklist(
    hass: HomeAssistant, enable_bluetooth, caplog
):
    caplog.set_level(logging.DEBUG)
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    first, second = proxies(2)
    ctx, kwargs = connections(refused(), refused(), Linked(cfg=SAMPLE))
    pair = AsyncMock(return_value="passkey answered")
    with (
        ctx,
        patch("custom_components.pvvx_ota.config_flow.pairing_proxies", return_value=[first, second]),
        patch("custom_components.pvvx_ota.config_flow.async_pair", pair),
    ):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
        r = await submit(hass, r["flow_id"], SECRET_PIN)
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": "pin_wrong"}
    assert [c.args[1] for c in pair.await_args_list] == [first]  # the second proxy is never asked
    assert kwargs[1:] == [{"encrypt": True, "bonded_scanner": first.source}]
    assert "pairing through proxy-0 finished but the sensor still refuses" in caplog.text
    assert str(SECRET_PIN) not in caplog.text


async def test_a_refusing_sensor_twice_leaves_the_repair_issue(hass: HomeAssistant, enable_bluetooth):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    ctx, _ = connections(refused(), refused(), refused())
    with (
        ctx,
        patch("custom_components.pvvx_ota.config_flow.pairing_proxies", return_value=proxies(3)),
        patch("custom_components.pvvx_ota.config_flow.async_pair", AsyncMock(return_value="no passkey asked")),
    ):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
        r = await submit(hass, r["flow_id"], SECRET_PIN)
        assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": "pair_no_passkey"}
        assert ir.async_get(hass).async_get_issue(DOMAIN, f"pin_pairing_failed_{MAC}") is None
        r = await submit(hass, r["flow_id"], SECRET_PIN)
        assert r["errors"] == {"base": "pair_no_passkey"}
    assert ir.async_get(hass).async_get_issue(
        DOMAIN, f"pin_pairing_failed_{MAC}"
    )  # the second failed attempt leaves it


async def test_a_pairer_that_cannot_connect_or_a_service_error_ends_the_attempt_with_its_own_error(
    hass: HomeAssistant, enable_bluetooth
):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    ctx, _ = connections(refused())
    errors = iter([pairing.PairConnectError("proxy-1", MAC), HomeAssistantError("service gone")])

    async def pair(*args):
        raise next(errors)

    with (
        ctx,
        patch("custom_components.pvvx_ota.config_flow.pairing_proxies", return_value=proxies(2)),
        patch("custom_components.pvvx_ota.config_flow.async_pair", pair),
    ):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
        r = await submit(hass, r["flow_id"], SECRET_PIN)
        assert r["errors"] == {"base": "pair_connect_failed"}
        r = await submit(hass, r["flow_id"], SECRET_PIN)
        assert r["errors"] == {"base": "cannot_connect"}


async def test_the_progress_step_shows_while_the_pairing_runs(hass: HomeAssistant, enable_bluetooth):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    release = asyncio.Event()

    async def slow_pair(*args):
        await release.wait()
        return "passkey answered"

    ctx, _ = connections(refused(), Linked(cfg=SAMPLE))
    with (
        ctx,
        patch("custom_components.pvvx_ota.config_flow.pairing_proxies", return_value=proxies(1)),
        patch("custom_components.pvvx_ota.config_flow.async_pair", slow_pair),
        patch("custom_components.pvvx_ota.async_setup_entry", return_value=True),
    ):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {CONF_PIN: SECRET_PIN})
        assert r["type"] is FlowResultType.SHOW_PROGRESS and r["progress_action"] == "pairing"
        r = await hass.config_entries.flow.async_configure(r["flow_id"])  # asked again while it still runs
        assert r["type"] is FlowResultType.SHOW_PROGRESS
        release.set()
        await hass.async_block_till_done()
        r = await hass.config_entries.flow.async_configure(r["flow_id"])
    assert r["type"] is FlowResultType.CREATE_ENTRY


# -- hassfest: no URLs in the strings, so the flasher link comes in as a placeholder --------------------------


def test_the_strings_carry_no_urls_and_the_flasher_link_is_a_placeholder():
    import json
    import re
    from pathlib import Path

    from custom_components.pvvx_ota.const import FLASHER_URL

    base = Path(__file__).parent.parent / "custom_components" / "pvvx_ota"
    for name in ("strings.json", "translations/en.json", "translations/de.json"):
        text = (base / name).read_text(encoding="utf-8")
        assert not re.search(r"https?://", text), f"{name} contains a URL"
        data = json.loads(text)
        for where in (data["config"]["error"]["pin_wrong"], data["config"]["error"]["pair_no_passkey"]):
            assert "{flasher_url}" in where and "{name}" in where
        assert "{flasher_url}" in data["issues"]["pin_pairing_failed"]["description"]
    assert FLASHER_URL.startswith("https://") and FLASHER_URL.endswith("TelinkMiFlasher.html")


async def test_the_flasher_link_reaches_the_form_and_the_repair_issue(hass: HomeAssistant, enable_bluetooth):
    from custom_components.pvvx_ota.const import FLASHER_URL

    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=svc())
    ctx, _ = connections(refused(), refused(), refused())
    with (
        ctx,
        patch("custom_components.pvvx_ota.config_flow.pairing_proxies", return_value=[PROXY_OFFER]),
        patch("custom_components.pvvx_ota.config_flow.async_pair", AsyncMock(return_value="passkey answered")),
    ):
        r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
        assert r["description_placeholders"]["flasher_url"] == FLASHER_URL  # the first form
        r = await submit(hass, r["flow_id"], SECRET_PIN)
        assert r["errors"] == {"base": "pin_wrong"} and r["description_placeholders"]["flasher_url"] == FLASHER_URL
        await submit(hass, r["flow_id"], SECRET_PIN)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"pin_pairing_failed_{MAC}")
    assert issue is not None and issue.translation_placeholders["flasher_url"] == FLASHER_URL


# -- forgetting the bond when the entry is removed ----------------------------------------------------------


def bonded_entry(hass):
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
            CONF_PIN: SECRET_PIN,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_removing_a_bonded_entry_asks_its_proxy_to_forget_the_bond(hass: HomeAssistant):
    calls: list[ServiceCall] = []

    async def handler(call: ServiceCall):
        calls.append(call)

    hass.services.async_register("esphome", "proxy_1_forget_pvvx", handler)
    entry = bonded_entry(hass)
    scanner = SimpleNamespace(name="proxy-1 (AA:BB:CC:DD:EE:FF)", source=PROXY)
    with patch.object(pairing.bluetooth, "async_current_scanners", return_value=[scanner]):
        assert await hass.config_entries.async_remove(entry.entry_id)
    assert [dict(c.data) for c in calls] == [{"mac": MAC}]  # the MAC only: never the PIN


async def test_an_older_package_without_the_action_leaves_the_bond_and_says_so(hass: HomeAssistant, caplog):
    caplog.set_level(logging.INFO)
    entry = bonded_entry(hass)
    scanner = SimpleNamespace(name="proxy-1", source=PROXY)
    with patch.object(pairing.bluetooth, "async_current_scanners", return_value=[scanner]):
        assert await hass.config_entries.async_remove(entry.entry_id)  # removal itself never fails
    assert "bond" in caplog.text and "stays on it" in caplog.text
    assert str(SECRET_PIN) not in caplog.text


async def test_forgetting_survives_a_missing_proxy_and_a_failing_call(hass: HomeAssistant):
    assert await pairing.async_forget_bond(hass, MAC, PROXY) is False  # no such scanner
    hass.services.async_register("esphome", "proxy_1_forget_pvvx", AsyncMock(side_effect=RuntimeError("boom")))
    scanner = SimpleNamespace(name="proxy-1", source=PROXY)
    with patch.object(pairing.bluetooth, "async_current_scanners", return_value=[scanner]):
        assert await pairing.async_forget_bond(hass, MAC, PROXY) is False


async def test_a_sensor_without_a_bond_calls_nothing_on_removal(hass: HomeAssistant):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, title="ATC", data={CONF_ADDRESS: MAC, CONF_VERSION: 0x59})
    entry.add_to_hass(hass)
    with patch.object(pairing, "async_forget_bond") as forget:
        assert await hass.config_entries.async_remove(entry.entry_id)
    forget.assert_not_called()
