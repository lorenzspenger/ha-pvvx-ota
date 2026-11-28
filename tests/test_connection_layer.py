"""device.async_connected against a fake Bluetooth layer, not a patched ``async_connected``.

Real: bleak_retry_connector.establish_connection, habluetooth's HaBleakClientWithServiceCache (backend choice, connect,
connection tracking, disconnect), bt_internals' pinned subclass, the entry lookup, encrypt/verify/re-pair logic.
Fake: the manager (scanner paths), the scanners with their backend factories (what an ESPHome proxy or adapter offers),
the sensor behind the backend, and the GATT calls of the client (no service discovery here). async_repair is replaced by
a function that makes the fake sensor accept the bond again, as the proxy's pairer would.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from bleak.backends.device import BLEDevice
from habluetooth import wrappers
from habluetooth.usage import HaBleakClientWithServiceCache
from homeassistant.const import CONF_ADDRESS
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pvvx_ota import bt_internals as bt
from custom_components.pvvx_ota import device as dev
from custom_components.pvvx_ota.const import CONF_BONDED_SCANNER, CONF_PIN, CONF_PIN_BONDED, DOMAIN
from custom_components.pvvx_ota.ota import PairingRequiredError
from custom_components.pvvx_ota.shared import get_shared

from .fake_gatt import FakeClient
from .test_config import MAC, SAMPLE
from .translated import raises_key

PROXY = "AA:BB:CC:00:00:01"
OTHER = "AA:BB:CC:00:00:02"
PIN = 424242


class FakeScanner:
    """What habluetooth's BaseHaScanner offers a connecting client (a remote scanner with a connector)."""

    def __init__(self, source: str, backends: list):
        self.source = source
        self.name = f"proxy {source[-2:]} ({source})"
        self.connectable = True
        self.adapter_idx = None
        self._clients: set = set()
        self.connecting: list[str] = []
        self.connector = SimpleNamespace(client=self._new_backend, can_connect=lambda: True)
        self._backends = backends

    def _new_backend(self, device, **kwargs):
        return self._backends.pop(0)(self.source)

    def _add_connecting(self, address):
        self.connecting.append(address)

    def _finished_connecting(self, address, connected):
        pass

    def get_discovered_device_advertisement_data(self, address):
        return None

    def connection_failures(self, address):
        return 0

    def connections_in_progress(self):
        return 0

    def get_allocations(self):
        return None


class FakeManager:
    shutdown = False

    def __init__(self):
        self.scanners: dict[str, FakeScanner] = {}
        self.rssi: dict[str, int] = {}
        self.pending: list = []  # one factory per connection, in order: source -> backend (a FakeClient)
        self.requested: list[str] = []  # the scanner each connection was handed to

    def add_scanner(self, source: str, rssi: int) -> FakeScanner:
        scanner = FakeScanner(source, self.pending)
        self.scanners[source] = scanner
        self.rssi[source] = rssi
        return scanner

    def paths(self, address, connectable=True):
        return [
            SimpleNamespace(
                scanner=s,
                ble_device=BLEDevice(address, "ATC_000001", {"source": s.source}),
                advertisement=SimpleNamespace(rssi=self.rssi[s.source]),
                score_connection_path=lambda diff, _s=s: float(self.rssi[_s.source]),
            )
            for s in self.scanners.values()
        ]

    # habluetooth.BluetoothManager methods the wrapper uses
    def async_scanner_devices_by_address(self, address, connectable):
        return self.paths(address, connectable)

    def async_scanner_by_source(self, source):
        return self.scanners.get(source)

    def async_current_scanners(self):
        return list(self.scanners.values())

    def async_release_connection_slot(self, device):
        pass

    def async_allocate_connection_slot(self, device):
        return True


class Sensor(FakeClient):
    """One connection to the fake sensor: the GATT side of the client the integration gets."""

    def __init__(self, link, source):
        super().__init__(cfg=SAMPLE)
        self.link = link
        self.source = source
        self.connected_flag = False
        self.needs_pairing = link.mode in ("refuse", "pair")
        self.no_reply = link.mode == "silent"
        self.pair_helps = link.mode in ("ok", "pair")  # refuse/silent: a stale bond, only a new pairing helps

    async def connect(self, pair=False, **kwargs):
        self.connected_flag = True
        self.link.connects.append(self.source)

    async def disconnect(self):
        self.connected_flag = False
        self.link.disconnects += 1


class SensorLink:
    """State of the sensor behind the proxies: does it accept the bond, and what happened to it."""

    def __init__(self):
        self.mode = "ok"  # ok | pair (refuses until pair() encrypts) | refuse (ATT error 15) | silent (no reply)
        self.connects: list[str] = []
        self.disconnects = 0
        self.sensors: list[Sensor] = []

    def backend(self, source):
        sensor = Sensor(self, source)
        self.sensors.append(sensor)
        return sensor


class FakeHaClient(HaBleakClientWithServiceCache):
    """Real habluetooth client; only the GATT calls go to the fake sensor (service discovery is not modelled)."""

    async def pair(self, *a, **k):
        return await self._backend.pair()

    async def start_notify(self, char, cb, **k):
        await self._backend.start_notify(char, cb)

    async def stop_notify(self, char):
        await self._backend.stop_notify(char)

    async def write_gatt_char(self, char, data, response=None):
        await self._backend.write_gatt_char(char, data, response)

    async def read_gatt_char(self, char):
        return await self._backend.read_gatt_char(char)


@pytest.fixture
def world(monkeypatch):
    manager = FakeManager()
    link = SensorLink()
    manager.link = link
    monkeypatch.setattr(wrappers, "get_manager", lambda: manager)
    monkeypatch.setattr(dev, "BleakClientWithServiceCache", FakeHaClient)
    monkeypatch.setattr(bt, "BleakClientWithServiceCache", FakeHaClient)
    monkeypatch.setattr(
        dev.bluetooth,
        "async_ble_device_from_address",
        lambda hass, address, connectable=True: (
            max(
                manager.paths(address), key=lambda p: p.advertisement.rssi, default=SimpleNamespace(ble_device=None)
            ).ble_device
        ),
    )
    monkeypatch.setattr(
        dev.bluetooth, "async_scanner_devices_by_address", lambda hass, a, connectable=True: manager.paths(a)
    )
    monkeypatch.setattr(dev, "ENCRYPT_SETTLE", 0.05)
    monkeypatch.setattr(dev, "PROBE_TIMEOUT", 0.05)
    monkeypatch.setattr("custom_components.pvvx_ota.ota.CFG_TIMEOUT", 0.05)
    repairs: list[tuple] = []

    async def repair(hass, address, source, pin):
        repairs.append((address, source, pin))
        link.mode = "ok"
        return True

    monkeypatch.setattr(dev, "async_repair", repair)
    manager.repairs = repairs
    manager.expect = lambda n: manager.pending.extend([link.backend] * n)
    return manager


def bonded_entry(hass, pin=PIN):
    data = {CONF_ADDRESS: MAC, CONF_PIN_BONDED: True, CONF_BONDED_SCANNER: PROXY}
    if pin is not None:
        data[CONF_PIN] = pin
    MockConfigEntry(domain=DOMAIN, unique_id=MAC, data=data).add_to_hass(hass)


async def test_plain_sensor_connects_through_the_strongest_path_without_encryption(hass, world):
    world.add_scanner(PROXY, -80)
    world.add_scanner(OTHER, -50)
    world.expect(1)
    async with dev.async_connected(hass, MAC, "ATC") as client:
        assert client.is_connected
        assert dev.connected_source(client) == OTHER  # real habluetooth choice: the stronger path
        assert get_shared(hass).links[MAC].startswith("proxy 02")
    sensor = world.link.sensors[0]
    assert sensor.pair_calls == 0 and not sensor.connected_flag and world.link.disconnects == 1
    assert world.link.connects == [OTHER]


async def test_bonded_sensor_goes_through_the_bonded_proxy_although_another_is_stronger(hass, world):
    bonded_entry(hass)
    world.add_scanner(PROXY, -80)
    world.add_scanner(OTHER, -40)
    world.link.mode = "pair"  # a plain link would be refused: only pair() on the paired proxy encrypts it
    world.expect(1)
    async with dev.async_connected(hass, MAC, "ATC") as client:
        assert dev.connected_source(client) == PROXY and world.link.connects == [PROXY]
        assert client._backend.pair_calls == 1  # encrypted before the first command
    assert world.repairs == [] and world.link.disconnects == 1


async def test_bonded_sensor_seen_only_by_another_proxy_is_refused_before_connecting(hass, world):
    bonded_entry(hass)
    world.add_scanner(OTHER, -40)
    world.expect(1)
    with raises_key("paired_proxy_does_not_see_sensor", proxy=PROXY):
        async with dev.async_connected(hass, MAC, "ATC"):
            pytest.fail("must not get a client")
    assert world.link.connects == [] and world.link.sensors == [] and len(world.pending) == 1


async def test_a_refused_link_is_paired_again_once_and_works(hass, world):
    bonded_entry(hass)
    world.add_scanner(PROXY, -60)
    world.link.mode = "refuse"
    # first connection: pair() cannot encrypt (stale bond) -> refusal on the probe; then the re-pair fixes the sensor
    world.expect(2)
    async with dev.async_connected(hass, MAC, "ATC") as client:
        assert client._backend is world.link.sensors[1]
    assert world.repairs == [(MAC, PROXY, PIN)]
    assert world.link.connects == [PROXY, PROXY] and world.link.disconnects == 2


async def test_a_refusal_that_pairing_again_does_not_cure_fails_after_one_attempt(hass, world, monkeypatch):
    bonded_entry(hass)
    world.add_scanner(PROXY, -60)
    world.link.mode = "refuse"

    async def useless(hass, address, source, pin):
        world.repairs.append((address, source, pin))
        return True  # the sensor still refuses

    monkeypatch.setattr(dev, "async_repair", useless)
    world.expect(2)
    with pytest.raises(PairingRequiredError):
        async with dev.async_connected(hass, MAC, "ATC"):
            pytest.fail("must not get a client")
    assert len(world.repairs) == 1 and world.link.disconnects == 2


async def test_one_silent_probe_changes_nothing_two_in_a_row_pair_again(hass, world):
    bonded_entry(hass)
    world.add_scanner(PROXY, -60)
    world.link.mode = "silent"
    world.expect(1)
    with pytest.raises(dev.SilentLinkError):
        async with dev.async_connected(hass, MAC, "ATC"):
            pytest.fail("must not get a client")
    assert world.repairs == [] and get_shared(hass).silent_probes == {MAC: 1}
    assert world.link.disconnects == 1

    world.expect(2)  # second silent probe: the bond is given up, the connection after the re-pair works
    async with dev.async_connected(hass, MAC, "ATC") as client:
        assert client.is_connected
    assert world.repairs == [(MAC, PROXY, PIN)] and MAC not in get_shared(hass).silent_probes


async def test_a_dead_bond_without_a_stored_pin_starts_reauth_and_pairs_nothing(hass, world):
    MockConfigEntry(domain=DOMAIN, unique_id="other", data={CONF_ADDRESS: "11:22:33:44:55:66"}).add_to_hass(hass)
    bonded_entry(hass, pin=None)  # the entry of this sensor is found among others
    world.add_scanner(PROXY, -60)
    world.link.mode = "refuse"
    world.expect(1)
    with pytest.raises(PairingRequiredError, match="no PIN stored"):
        async with dev.async_connected(hass, MAC, "ATC"):
            pytest.fail("must not get a client")
    assert world.repairs == [] and world.link.disconnects == 1
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_entry_lookup_finds_only_the_entry_of_that_address(hass):
    assert dev.entry_for(hass, MAC) is None
    MockConfigEntry(domain=DOMAIN, unique_id="other", data={CONF_ADDRESS: "11:22:33:44:55:66"}).add_to_hass(hass)
    assert dev.entry_for(hass, MAC) is None
    bonded_entry(hass)
    assert dev.entry_for(hass, MAC.lower()).unique_id == MAC


async def test_a_cancelled_probe_still_drops_the_link(hass, world, monkeypatch):
    bonded_entry(hass)
    world.add_scanner(PROXY, -60)
    world.link.mode = "pair"
    world.expect(1)

    async def cancelled(client, timeout):
        raise asyncio.CancelledError

    monkeypatch.setattr(dev, "read_cfg_raw", cancelled)
    with pytest.raises(asyncio.CancelledError):
        async with dev.async_connected(hass, MAC, "ATC"):
            pytest.fail("must not get a client")
    assert world.link.disconnects == 1 and not world.link.sensors[0].connected_flag
