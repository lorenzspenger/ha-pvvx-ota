"""Configuration block: layout, read-modify-write, entities, batching."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pvvx_ota import config as cfg
from custom_components.pvvx_ota import get_shared, ota
from custom_components.pvvx_ota.const import CONF_HW_ID, CONF_VERSION, DOMAIN

from .fake_gatt import FakeClient
from .test_ha import MAC, patch_catalog
from .translated import raises_key

# 0x55, fw 5.9, flg, flg2, flg3, event_adv_cnt, adv(1 s), meas(4), rf(169 = 0.04 dBm), latency, step, hw(LYWSD03MMC), avg
# flg 0xA3 = lp_measures | show_batt | advertising_type 3 (BTHome); flg2 0x05 = smiley 5
SAMPLE = bytes([0x55, 0x59, 0xA3, 0x05, 0x04, 0x06, 0x10, 0x04, 0xA9, 0x13, 0x37, 0x0A, 0x00])


# -- pure layout ------------------------------------------------------------------
def test_decode_known_sample():
    assert cfg.is_verified(SAMPLE)
    assert cfg.get(SAMPLE, "adv_interval") == 16 and cfg.get(SAMPLE, "measure_interval") == 4
    assert cfg.get(SAMPLE, "rf_tx_power") == 169 and cfg.get(SAMPLE, "smiley") == 5
    assert cfg.get(SAMPLE, "show_battery") == 1 and cfg.get(SAMPLE, "temp_f") == 0
    assert cfg.get(SAMPLE, "comfort_smiley") == 0 and cfg.get(SAMPLE, "screen_off") == 0


def test_write_payload_has_no_version_byte_and_keeps_everything_else():
    out = cfg.apply(SAMPLE, {"temp_f": 1, "screen_off": 1}, hw_id=10)
    assert len(out) == len(SAMPLE) - 2
    new = bytes([0x55, 0x59]) + out
    # only the two targeted bits differ; advertising_type (BTHome), hw_ver, flg3, event_adv_cnt untouched
    assert new[2] == 0xA3 | 0x10 and new[3] == 0x05 | 0x80
    assert new[4:] == SAMPLE[4:] and out[0] & 0x03 == 0x03


def test_changing_one_byte_field():
    out = cfg.apply(SAMPLE, {"adv_interval": 32, "smiley": 2}, hw_id=10)
    new = bytes([0x55, 0x59]) + out
    assert new[6] == 32 and new[3] == 0x02 and new[2] == SAMPLE[2] and new[7:] == SAMPLE[7:]


@pytest.mark.parametrize("ver", [0x56, 0x5A, 0x41, 0x60])
def test_unverified_firmware_refused(ver):
    raw = bytes([0x55, ver]) + SAMPLE[2:]
    assert not cfg.is_verified(raw) and cfg.capabilities(raw, 10) == set()
    with pytest.raises(cfg.ConfigError, match="not verified"):
        cfg.apply(raw, {"adv_interval": 20}, hw_id=10)


def test_short_or_garbled_block_refused():
    with pytest.raises(cfg.ConfigError):
        cfg.apply(SAMPLE[:8], {"adv_interval": 20}, hw_id=10)
    with pytest.raises(cfg.ConfigError):
        cfg.apply(b"\x00" + SAMPLE[1:], {"adv_interval": 20}, hw_id=10)


GENERIC = {"adv_interval", "measure_interval", "rf_tx_power", "adv_type", "adv_crypto", "bt5phy", "longrange"}
LY_DISPLAY = {"comfort_smiley", "temp_f", "show_battery", "smiley", "screen_off"}
MJ_DISPLAY = {"comfort_smiley", "x100", "temp_f", "time_am_pm", "screen_type", "screen_off", "not_day_of_week"}
# MJWSD05MMC (hw 9) block: flg 0xA3 = lp_measures | time_am_pm | BTHome; flg2 0x61 = longrange | bt5phy | screen_type 1;
# flg3 0x8A = not_day_of_week | adv_interval_delay 10
MJ_SAMPLE = bytes([0x55, 0x59, 0xA3, 0x61, 0x8A, 0x06, 0x10, 0x04, 0xA9, 0x13, 0x37, 0x09, 0x00])


@pytest.mark.parametrize(
    "hw,display",
    [(i, LY_DISPLAY) for i in (0, 3, 4, 5, 10, 14)] + [(9, MJ_DISPLAY), (12, MJ_DISPLAY | {"date_ddmm"})],
)
def test_display_fields_per_board(hw, display):
    assert cfg.capabilities(SAMPLE, hw) == GENERIC | display


@pytest.mark.parametrize("hw", [1, 2, 6, 7, 8, 11, 13])
def test_other_display_boards_get_only_unit_and_screen_off(hw):
    assert cfg.capabilities(SAMPLE, hw) == GENERIC | {"temp_f", "screen_off"}
    out = cfg.apply(SAMPLE, {"temp_f": 1, "screen_off": 1}, hw_id=hw)
    assert payload_diff(SAMPLE, out) == {2: (0xA3, 0xB3), 3: (0x05, 0x85)}  # flg bit 4, flg2 bit 7 only
    for key in ("comfort_smiley", "show_battery", "smiley", "screen_type", "x100"):
        with pytest.raises(cfg.ConfigError, match="cannot be changed"):
            cfg.apply(SAMPLE, {key: 1}, hw_id=hw)


@pytest.mark.parametrize("hw", [15, 16, 17, 49, 62, 99, None])
def test_unlisted_boards_get_no_display_fields(hw):
    """Screenless / different-layout boards (and unknown ids) only get the generic fields."""
    assert cfg.capabilities(SAMPLE, hw) == GENERIC
    with pytest.raises(cfg.ConfigError, match="cannot be changed"):
        cfg.apply(SAMPLE, {"temp_f": 1}, hw_id=hw)


def test_board_field_sets_never_share_bits():
    """Keys that sit on the same bits (show_battery/time_am_pm, smiley/screen_type) must never be on one board."""
    for hw in cfg.DISPLAY_BY_HW:
        used: dict[tuple[int, int], str] = {}
        for key in cfg.capabilities(SAMPLE, hw):
            f = cfg.FIELDS[key]
            for bit in range(8):
                if f.mask >> bit & 1:
                    assert (f.index, bit) not in used, (hw, key, used[(f.index, bit)])
                    used[(f.index, bit)] = key


def test_mjwsd05mmc_does_not_get_lywsd03mmc_bits():
    for key in ("show_battery", "smiley"):
        with pytest.raises(cfg.ConfigError, match="cannot be changed"):
            cfg.apply(MJ_SAMPLE, {key: 1}, hw_id=9)
    with pytest.raises(cfg.ConfigError, match="cannot be changed"):
        cfg.apply(MJ_SAMPLE, {"date_ddmm": 1}, hw_id=9)  # only the en board shows it
    with pytest.raises(cfg.ConfigError, match="cannot be changed"):
        cfg.apply(SAMPLE, {"screen_type": 1}, hw_id=10)


def payload_diff(old: bytes, payload: bytes) -> dict[int, tuple[int, int]]:
    """byte index (in the 0x55 block) -> (old, new) for every changed byte."""
    new = old[:2] + payload
    assert len(new) == len(old)
    return {i: (old[i], new[i]) for i in range(len(old)) if old[i] != new[i]}


@pytest.mark.parametrize(
    "key,value,index,old,new",
    [
        ("time_am_pm", 0, 2, 0xA3, 0x83),  # flg bit 5
        ("x100", 1, 2, 0xA3, 0xAB),  # flg bit 3
        ("temp_f", 1, 2, 0xA3, 0xB3),  # flg bit 4
        ("comfort_smiley", 1, 2, 0xA3, 0xA7),  # flg bit 2
        ("screen_type", 4, 3, 0x61, 0x64),  # flg2 bits 0..2 only
        ("screen_off", 1, 3, 0x61, 0xE1),  # flg2 bit 7
        ("not_day_of_week", 0, 4, 0x8A, 0x0A),  # flg3 bit 7, the delay nibble stays 10
    ],
)
def test_mjwsd05mmc_read_modify_write_changes_only_its_bits(key, value, index, old, new):
    assert payload_diff(MJ_SAMPLE, cfg.apply(MJ_SAMPLE, {key: value}, 9)) == {index: (old, new)}


def test_mjwsd05mmc_en_ddmm_is_flg3_bit6():
    assert payload_diff(MJ_SAMPLE, cfg.apply(MJ_SAMPLE, {"date_ddmm": 1}, 12)) == {4: (0x8A, 0xCA)}


def test_mjwsd05mmc_get_reads_the_right_bits():
    got = {k: cfg.get(MJ_SAMPLE, k) for k in MJ_DISPLAY}
    assert got == {
        "comfort_smiley": 0,
        "x100": 0,
        "temp_f": 0,
        "time_am_pm": 1,
        "screen_type": 1,
        "screen_off": 0,
        "not_day_of_week": 1,
    }


def test_lywsd03mmc_display_write_keeps_other_bits():
    out = cfg.apply(SAMPLE, {"show_battery": 0, "smiley": 2}, hw_id=10)
    assert payload_diff(SAMPLE, out) == {2: (0xA3, 0x83), 3: (0x05, 0x02)}


def test_screen_type_limit():
    for v in (6, 7):
        with pytest.raises(cfg.ConfigError, match="outside 0..5"):
            cfg.apply(MJ_SAMPLE, {"screen_type": v}, hw_id=9)


@pytest.mark.parametrize(
    "key,val", [("adv_interval", 0), ("adv_interval", 161), ("measure_interval", 1), ("smiley", 8)]
)
def test_out_of_range_refused(key, val):
    with pytest.raises(cfg.ConfigError, match="outside"):
        cfg.apply(SAMPLE, {key: val}, hw_id=10)


def test_snapshot_lists_usable_fields():
    snap = cfg.snapshot(SAMPLE, 10)
    assert snap["adv_interval"] == 16 and snap["smiley"] == 5 and "adv_crypto" not in snap
    assert set(snap) == cfg.capabilities(SAMPLE, 10) - {"adv_crypto"}


# -- ota transport -------------------------------------------------------------------
async def test_write_cfg_returns_device_reply_and_read_cfg_raw():
    c = FakeClient(cfg=SAMPLE)
    assert await ota.read_cfg_raw(c) == SAMPLE
    payload = cfg.apply(SAMPLE, {"adv_interval": 20}, 10)
    echoed = await ota.write_cfg(c, payload)
    assert c.cfg_writes == [b"\x55" + payload] and echoed[6] == 20 and echoed[:2] == SAMPLE[:2]


# -- entities ------------------------------------------------------------------------
def connection(client_factory, calls):
    @asynccontextmanager
    async def fake(hass, address, name):
        calls.append(address)
        client = client_factory()
        try:
            yield client
        finally:
            await client.disconnect()

    return fake


@pytest.fixture
def fast_batch(monkeypatch):
    monkeypatch.setattr("custom_components.pvvx_ota.store.BATCH_WINDOW", 0.05)
    pass


async def setup_cfg_entry(hass, client, hw=10, version=0x59, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC,
        title="ATC_000001",
        options=options or {},
        data={CONF_ADDRESS: MAC, CONF_VERSION: version, CONF_HW_ID: hw},
    )
    entry.add_to_hass(hass)
    calls: list[str] = []
    fake = connection(lambda: client, calls)
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
    await hass.async_block_till_done()
    store = getattr(entry, "runtime_data", None)
    if store is not None and store.first_read is not None:
        await store.first_read  # the background read (setup itself makes no connection)
        await hass.async_block_till_done()
    return entry, calls, ctx


def ent(hass, domain, key):
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{MAC}_{key}")


async def test_entities_created_and_in_config_category(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        reg = er.async_get(hass)
        for dom, key in [
            ("number", "adv_interval"),
            ("number", "measure_interval"),
            ("select", "rf_tx_power"),
            ("select", "temp_unit_display"),
            ("select", "smiley"),
            ("switch", "show_battery"),
            ("switch", "comfort_smiley"),
            ("switch", "screen_off"),
            ("button", "refresh_config"),
            ("button", "restore_config"),
        ]:
            eid = ent(hass, dom, key)
            assert eid, (dom, key)
            assert reg.async_get(eid).entity_category == "config"
        assert hass.states.get(ent(hass, "number", "adv_interval")).state == "1.0"
        assert hass.states.get(ent(hass, "number", "measure_interval")).state == "4"
        assert hass.states.get(ent(hass, "select", "smiley")).state == "frame happy"
        assert hass.states.get(ent(hass, "select", "rf_tx_power")).state == "+0.04 dBm"
        assert hass.states.get(ent(hass, "switch", "show_battery")).state == "on"
        assert hass.states.get(ent(hass, "switch", "screen_off")).state == "off"
        assert len(calls) == 1  # the version read at setup also fills the config: no extra connection
    finally:
        [c.stop() for c in ctx]


MJ_DISPLAY_ENTITIES = [
    ("select", "temp_unit_display"),
    ("select", "screen_type"),
    ("switch", "comfort_smiley"),
    ("switch", "screen_off"),
    ("switch", "x100"),
    ("switch", "time_am_pm"),
    ("switch", "show_weekday"),
]


@pytest.mark.real_defaults
async def test_mjwsd05mmc_gets_its_own_display_entities_disabled(hass: HomeAssistant, enable_bluetooth, fast_batch):
    """An existing entry (hardware id already stored) gets the new entities at the next setup, disabled."""
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=MJ_SAMPLE), hw=9)
    try:
        reg = er.async_get(hass)
        assert ent(hass, "number", "adv_interval") and ent(hass, "select", "rf_tx_power")
        for dom, key in MJ_DISPLAY_ENTITIES:
            eid = ent(hass, dom, key)
            assert eid, (dom, key)
            assert reg.async_get(eid).disabled_by == er.RegistryEntryDisabler.INTEGRATION, key
        # LYWSD03MMC-only keys and the en-only date format are not created for this board
        for dom, key in [("select", "smiley"), ("switch", "show_battery"), ("switch", "date_ddmm")]:
            assert ent(hass, dom, key) is None, key
    finally:
        [c.stop() for c in ctx]


async def test_mjwsd05mmc_en_also_gets_date_format(hass: HomeAssistant, enable_bluetooth, fast_batch):
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=MJ_SAMPLE[:11] + bytes([12, 0])), hw=12)
    try:
        assert ent(hass, "switch", "date_ddmm") and ent(hass, "switch", "show_weekday")
    finally:
        [c.stop() for c in ctx]


async def test_mjwsd05mmc_display_entities_read_and_write_their_bits(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=MJ_SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=9)
    try:
        for dom, key in MJ_DISPLAY_ENTITIES:
            await enable(hass, ent(hass, dom, key))
        st = hass.states.get
        assert st(ent(hass, "select", "screen_type")).state == "temperature"
        assert st(ent(hass, "switch", "time_am_pm")).state == "on"
        assert st(ent(hass, "switch", "x100")).state == "off"
        assert st(ent(hass, "switch", "show_weekday")).state == "off"  # not_day_of_week is set: inverted
        # turning "show weekday" on clears flg3 bit 7 and leaves the advertising delay alone
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": ent(hass, "switch", "show_weekday")}, blocking=True
        )
        assert client.cfg == MJ_SAMPLE[:4] + bytes([0x0A]) + MJ_SAMPLE[5:]
        assert st(ent(hass, "switch", "show_weekday")).state == "on"
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": ent(hass, "select", "screen_type"), "option": "battery V"},
            blocking=True,
        )
        assert client.cfg[3] == 0x64 and client.cfg[2] == 0xA3
        assert [e for e in client.cfg_writes][-1][0] == 0x55
    finally:
        [c.stop() for c in ctx]


@pytest.mark.parametrize("hw", [17, 49, 99])
async def test_unlisted_hardware_gets_no_display_entities(hass: HomeAssistant, enable_bluetooth, fast_batch, hw):
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE[:11] + bytes([hw, 0])), hw=hw)
    try:
        assert ent(hass, "number", "adv_interval")
        for dom, key in MJ_DISPLAY_ENTITIES + [("select", "smiley"), ("switch", "show_battery")]:
            assert ent(hass, dom, key) is None, (hw, key)
    finally:
        [c.stop() for c in ctx]


async def test_unverified_version_creates_no_config_entities(hass: HomeAssistant, enable_bluetooth, fast_batch):
    old = bytes([0x55, 0x56]) + SAMPLE[2:]
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=old), version=0x56)
    try:
        assert ent(hass, "number", "adv_interval") is None and ent(hass, "button", "refresh_config")
    finally:
        [c.stop() for c in ctx]


async def test_set_number_is_read_modify_write(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        # someone changed the sensor elsewhere (TelinkMiFlasher) since the setup read: show_battery off
        client.cfg = bytes([0x55, 0x59, 0x83]) + SAMPLE[3:]
        await hass.services.async_call(
            "number", "set_value", {"entity_id": ent(hass, "number", "adv_interval"), "value": 2.5}, blocking=True
        )
        assert len(client.cfg_writes) == 1
        w = client.cfg_writes[0]
        assert (
            w[0] == 0x55 and w[1:] == bytes([0x83]) + SAMPLE[3:6] + bytes([40]) + SAMPLE[7:]
        )  # fresh bytes kept, 2.5 s = 40
        assert w[1] & 0x03 == 0x03  # still BTHome
        assert hass.states.get(ent(hass, "number", "adv_interval")).state == "2.5"
        assert hass.states.get(ent(hass, "switch", "show_battery")).state == "off"  # picked up from the fresh read
    finally:
        [c.stop() for c in ctx]


async def test_changes_within_window_share_one_connection(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        calls.clear()
        await asyncio.gather(
            hass.services.async_call(
                "switch", "turn_on", {"entity_id": ent(hass, "switch", "screen_off")}, blocking=True
            ),
            hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": ent(hass, "select", "temp_unit_display"), "option": "°F"},
                blocking=True,
            ),
            hass.services.async_call(
                "number", "set_value", {"entity_id": ent(hass, "number", "measure_interval"), "value": 8}, blocking=True
            ),
        )
        assert len(calls) == 1 and len(client.cfg_writes) == 1
        assert cfg.get(client.cfg, "screen_off") == 1 and cfg.get(client.cfg, "temp_f") == 1
        assert cfg.get(client.cfg, "measure_interval") == 8
    finally:
        [c.stop() for c in ctx]


async def test_clamped_value_reports_what_the_sensor_stored(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)

    def clamp(b):
        b = bytearray(b)
        b[7] = min(b[7], 10)  # firmware limits measure interval here
        return bytes(b)

    client.clamp = clamp
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        with pytest.raises(HomeAssistantError, match="stored"):
            await hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": ent(hass, "number", "measure_interval"), "value": 20},
                blocking=True,
            )
        assert hass.states.get(ent(hass, "number", "measure_interval")).state == "10"
    finally:
        [c.stop() for c in ctx]


async def test_write_refused_when_sensor_reports_unverified_firmware(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        client.cfg = bytes([0x55, 0x5A]) + SAMPLE[2:]  # flashed with something newer behind our back
        with pytest.raises(HomeAssistantError, match="not verified"):
            await hass.services.async_call(
                "number", "set_value", {"entity_id": ent(hass, "number", "adv_interval"), "value": 2}, blocking=True
            )
        assert client.cfg_writes == []
    finally:
        [c.stop() for c in ctx]


async def test_no_write_while_flashing(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        get_shared(hass).flashing = True
        store = entry.runtime_data
        store.async_notify()  # what _async_flash does
        await hass.async_block_till_done()
        assert hass.states.get(ent(hass, "switch", "screen_off")).state == "unavailable"
        with raises_key("flash_running"):
            await store.async_request({"screen_off": 1})
        assert client.cfg_writes == [] and store.value("screen_off") == 0
    finally:
        get_shared(hass).flashing = False
        [c.stop() for c in ctx]


async def test_refresh_button_reads_without_writing(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        client.cfg = bytes([0x55, 0x59]) + cfg.apply(SAMPLE, {"smiley": 1}, 10)
        await hass.services.async_call(
            "button", "press", {"entity_id": ent(hass, "button", "refresh_config")}, blocking=True
        )
        assert client.cfg_writes == [] and hass.states.get(ent(hass, "select", "smiley")).state == "happy"
    finally:
        [c.stop() for c in ctx]


async def test_snapshot_before_flash_and_manual_restore(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        store = entry.runtime_data
        store.take_snapshot()  # what _async_flash does after its read
        assert entry.data["cfg_snapshot"]["fields"]["adv_interval"] == 16
        assert hass.states.get(ent(hass, "button", "restore_config")).state == "unavailable"  # nothing to restore yet
        # the flash reset some settings (and the new firmware also changed flg3, which we never touch)
        client.cfg = bytes([0x55, 0x59, 0x83, 0x00, 0x04, 0x06, 0x08, 0x04, 0xA9, 0x13, 0x37, 0x0A, 0x00])
        await hass.services.async_call(
            "button", "press", {"entity_id": ent(hass, "button", "refresh_config")}, blocking=True
        )
        assert hass.states.get(ent(hass, "button", "restore_config")).state != "unavailable"
        assert client.cfg_writes == []  # never restored automatically
        await hass.services.async_call(
            "button", "press", {"entity_id": ent(hass, "button", "restore_config")}, blocking=True
        )
        assert len(client.cfg_writes) == 1
        assert cfg.get(client.cfg, "adv_interval") == 16 and cfg.get(client.cfg, "smiley") == 5
        assert cfg.get(client.cfg, "show_battery") == 1
        assert hass.states.get(ent(hass, "button", "restore_config")).state == "unavailable"  # nothing left to restore
    finally:
        [c.stop() for c in ctx]


async def test_flash_takes_snapshot_and_blocks_config_writes(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE[:11] + bytes([0, 0]))  # hardware id 0 is in the test catalog
    seen = {}
    orig = client.write_gatt_char

    async def spy(char, data, response):
        seen.setdefault("ota", []).append(get_shared(hass).flashing) if char.endswith("2b12") else None
        return await orig(char, data, response)

    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        client.write_gatt_char = spy
        with patch("custom_components.pvvx_ota.update.async_call_later"):
            await hass.services.async_call(DOMAIN, "flash_latest", {"entity_id": "update.atc_000001"}, blocking=True)
        assert entry.data["cfg_snapshot"]["fields"]["adv_interval"] == 16  # taken before flashing
        assert seen["ota"] and all(seen["ota"])  # config writes were blocked while the image went over
        assert get_shared(hass).flashing is False
    finally:
        [c.stop() for c in ctx]


# -- dangerous settings ---------------------------------------------------------------
from homeassistant.exceptions import ServiceValidationError  # noqa: E402
from homeassistant.helpers import issue_registry as ir  # noqa: E402

UPDATE = "update.atc_000001"
KEY = "00112233445566778899aabbccddeeff"


def dump_everything(hass) -> str:
    """Every state with all attributes, as one string."""
    return " ".join(f"{s.entity_id} {s.state} {dict(s.attributes)}" for s in hass.states.async_all())


@pytest.mark.real_defaults
async def test_only_update_and_restore_are_enabled_by_default(hass: HomeAssistant, enable_bluetooth, fast_batch):
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        reg = er.async_get(hass)
        enabled = sorted(
            e.unique_id.split("_", 1)[1]
            for e in er.async_entries_for_config_entry(reg, entry.entry_id)
            if e.disabled_by is None
        )
        assert enabled == ["firmware", "restore_config"], enabled  # the update entity and the Restore button
        for dom, key in [
            ("select", "adv_type"),
            ("switch", "longrange"),
            ("switch", "bt5phy"),
            ("button", "reset_defaults"),
            ("sensor", "hw_ver"),
            ("number", "adv_interval"),
            ("number", "measure_interval"),
            ("select", "rf_tx_power"),
            ("switch", "beta_firmware"),
            ("switch", "show_battery"),
            ("switch", "comfort_smiley"),
            ("switch", "screen_off"),
            ("select", "temp_unit_display"),
            ("select", "smiley"),
            ("button", "refresh_config"),
            ("button", "sync_clock"),
            ("button", "reflash_firmware"),
            ("button", "reboot_sensor"),
        ]:
            eid = ent(hass, dom, key)
            assert eid, (dom, key)
            assert reg.async_get(eid).disabled_by == er.RegistryEntryDisabler.INTEGRATION, key
            assert hass.states.get(eid) is None
        assert reg.async_get(ent(hass, "button", "restore_config")).disabled_by is None
    finally:
        [c.stop() for c in ctx]


async def enable(hass, eid):
    er.async_get(hass).async_update_entity(eid, disabled_by=None)
    await hass.config_entries.async_reload(hass.config_entries.async_entries(DOMAIN)[0].entry_id)
    await hass.async_block_till_done()


async def test_adv_type_change_raises_repair_issue_and_clears(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        await enable(hass, ent(hass, "select", "adv_type"))
        eid = ent(hass, "select", "adv_type")
        assert hass.states.get(eid).state == "BTHome"
        await hass.services.async_call(
            "select", "select_option", {"entity_id": eid, "option": "pvvx custom"}, blocking=True
        )
        assert cfg.get(client.cfg, "adv_type") == 1
        assert ir.async_get(hass).async_get_issue(DOMAIN, f"adv_type_changed_{entry.entry_id}")
        await hass.services.async_call("select", "select_option", {"entity_id": eid, "option": "BTHome"}, blocking=True)
        assert ir.async_get(hass).async_get_issue(DOMAIN, f"adv_type_changed_{entry.entry_id}") is None
    finally:
        [c.stop() for c in ctx]


async def test_reads_clear_issues_whose_cause_is_gone(hass: HomeAssistant, enable_bluetooth, fast_batch):
    """Fixed elsewhere (another tool): the next read dismisses the issue."""
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        store, reg = entry.runtime_data, ir.async_get(hass)

        def raised(*keys):
            return {k: bool(reg.async_get_issue(DOMAIN, f"{k}_{entry.entry_id}")) for k in keys}

        keys = ("adv_type_changed", "encryption_restore_failed", "pin_set", "settings_differ_after_flash")

        def block(**changes):
            return bytes([0x55, 0x59]) + cfg.apply(SAMPLE, changes, 10, frozenset(changes) & {"adv_crypto"})

        for k in keys:
            store._issue(k)
        store.set_raw(block(adv_type=1))  # still not BTHome, encryption off; nothing to restore
        assert raised(*keys) == {
            "adv_type_changed": True,
            "encryption_restore_failed": True,
            "pin_set": False,  # a read without PIN
            "settings_differ_after_flash": False,
        }
        store.set_raw(block(adv_type=3, adv_crypto=1))
        assert not any(raised(*keys).values())
        # a bonded PIN sensor reads fine with its PIN: that says nothing about the issue
        hass.config_entries.async_update_entry(entry, data={**entry.data, "pin_bonded": True})
        store._issue("pin_set")
        store.set_raw(SAMPLE)
        assert raised("pin_set") == {"pin_set": True}
    finally:
        [c.stop() for c in ctx]


async def test_phy_switches_write_only_their_bits(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        await enable(hass, ent(hass, "switch", "longrange"))
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": ent(hass, "switch", "longrange")}, blocking=True
        )
        assert client.cfg[3] == SAMPLE[3] | 0x40 and client.cfg[2] == SAMPLE[2] and client.cfg[4:] == SAMPLE[4:]
    finally:
        [c.stop() for c in ctx]


async def test_pin_requires_confirm_and_never_leaks(hass: HomeAssistant, enable_bluetooth, fast_batch, caplog):
    caplog.set_level("DEBUG")
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        with pytest.raises(ServiceValidationError, match="confirm: true"):
            await hass.services.async_call(DOMAIN, "set_pin", {"entity_id": UPDATE, "pin": 123456}, blocking=True)
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN, "set_pin", {"entity_id": UPDATE, "pin": 123456, "confirm": False}, blocking=True
            )
        assert client.pin_writes == 0 and len(calls) == 1  # refused before any connection
        res = await hass.services.async_call(
            DOMAIN,
            "set_pin",
            {"entity_id": UPDATE, "pin": 424242, "confirm": True},
            blocking=True,
            return_response=True,
        )
        assert client.pin == 424242 and res[UPDATE] == {"pin_set": True}
        assert ir.async_get(hass).async_get_issue(DOMAIN, f"pin_set_{entry.entry_id}")
        assert "424242" not in caplog.text and "424242" not in dump_everything(hass)
        assert (424242).to_bytes(4, "little").hex() not in caplog.text
        assert "424242" not in str(entry.data)  # not stored
        # clearing needs no special flag but the same guard
        await hass.services.async_call(
            DOMAIN, "set_pin", {"entity_id": UPDATE, "pin": 0, "confirm": True}, blocking=True
        )
        assert client.pin == 0 and ir.async_get(hass).async_get_issue(DOMAIN, f"pin_set_{entry.entry_id}") is None
    finally:
        [c.stop() for c in ctx]


async def test_pin_validation_and_failure(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        import voluptuous as vol

        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN, "set_pin", {"entity_id": UPDATE, "pin": 1000000, "confirm": True}, blocking=True
            )
        client.pin_fails = True
        with raises_key("pin_not_stored"):
            await hass.services.async_call(
                DOMAIN, "set_pin", {"entity_id": UPDATE, "pin": 1234, "confirm": True}, blocking=True
            )
    finally:
        [c.stop() for c in ctx]


async def test_encryption_key_only_in_response(hass: HomeAssistant, enable_bluetooth, fast_batch, caplog):
    caplog.set_level("DEBUG")
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        res = await hass.services.async_call(
            DOMAIN, "set_encryption", {"entity_id": UPDATE, "enable": True}, blocking=True, return_response=True
        )
        key = res[UPDATE]["bindkey"]
        assert len(key) == 32 and bytes.fromhex(key) == client.bindkey and client.bindkey != bytes(16)
        assert cfg.get(client.cfg, "adv_crypto") == 1 and "BTHome" in res[UPDATE]["note"]
        assert cfg.get(client.cfg, "adv_type") == 3  # rest of the block untouched
        assert client.cfg[2] == SAMPLE[2] and client.cfg[4:] == SAMPLE[4:]
        assert key not in caplog.text and key not in dump_everything(hass) and key not in str(entry.data)
        assert key not in str(er.async_get(hass).entities.values())
        assert ir.async_get(hass).async_get_issue(DOMAIN, f"encryption_changed_{entry.entry_id}")
        # a given key is used as is, and also never leaks
        res = await hass.services.async_call(
            DOMAIN,
            "set_encryption",
            {"entity_id": UPDATE, "enable": True, "key": KEY},
            blocking=True,
            return_response=True,
        )
        assert res[UPDATE]["bindkey"] == KEY and client.bindkey.hex() == KEY
        assert KEY not in caplog.text
        # off: no key in the response
        res = await hass.services.async_call(
            DOMAIN, "set_encryption", {"entity_id": UPDATE, "enable": False}, blocking=True, return_response=True
        )
        assert "bindkey" not in res[UPDATE] and cfg.get(client.cfg, "adv_crypto") == 0
        with pytest.raises(HomeAssistantError, match="32 hex"):
            await hass.services.async_call(
                DOMAIN, "set_encryption", {"entity_id": UPDATE, "enable": True, "key": "abc"}, blocking=True
            )
    finally:
        [c.stop() for c in ctx]


async def test_encryption_flag_cannot_be_set_through_entities(hass: HomeAssistant, enable_bluetooth, fast_batch):
    with pytest.raises(cfg.ConfigError, match="set_encryption"):
        cfg.apply(SAMPLE, {"adv_crypto": 1}, 10)


@pytest.mark.parametrize(
    "service,data",
    [("set_pin", {"pin": 1234, "confirm": True}), ("set_encryption", {"enable": True})],
)
async def test_dangerous_actions_refused_on_unverified_firmware(
    hass: HomeAssistant, enable_bluetooth, fast_batch, service, data
):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        client.cfg = bytes([0x55, 0x5A]) + SAMPLE[2:]  # newer firmware than verified
        with pytest.raises(HomeAssistantError, match="not verified"):
            await hass.services.async_call(DOMAIN, service, {"entity_id": UPDATE, **data}, blocking=True)
        assert client.pin_writes == 0 and client.bindkey == bytes(16) and client.cfg_writes == []
    finally:
        [c.stop() for c in ctx]


async def test_reset_defaults_snapshot_readback_and_restore(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)
    client.default_cfg = bytes(
        [0x55, 0x59, 0x01, 0x00, 0x04, 0x06, 0x01, 0x03, 0xA9, 0x13, 0x37, 0x0A, 0x00]
    )  # pvvx type, defaults
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        await enable(hass, ent(hass, "button", "reset_defaults"))
        await hass.services.async_call(
            "button", "press", {"entity_id": ent(hass, "button", "reset_defaults")}, blocking=True
        )
        assert client.resets == 1 and client.cfg == client.default_cfg
        assert entry.data["cfg_snapshot"]["fields"]["adv_interval"] == 16  # saved before the reset
        store = entry.runtime_data
        assert cfg.get(store.raw, "adv_interval") == 1  # read back
        assert ir.async_get(hass).async_get_issue(DOMAIN, f"reset_defaults_{entry.entry_id}").is_fixable
        assert store.can_restore  # the restore button now offers the old settings
        await hass.services.async_call(
            "button", "press", {"entity_id": ent(hass, "button", "restore_config")}, blocking=True
        )
        assert cfg.get(client.cfg, "adv_interval") == 16 and cfg.get(client.cfg, "adv_type") == 3
        assert cfg.get(client.cfg, "adv_crypto") == 0  # never restores the encryption flag
        assert not ir.async_get(hass).async_get_issue(DOMAIN, f"reset_defaults_{entry.entry_id}")  # restored: cleared
    finally:
        [c.stop() for c in ctx]


async def test_reset_refused_on_unverified_firmware_and_while_flashing(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        store = entry.runtime_data
        client.cfg = bytes([0x55, 0x5A]) + SAMPLE[2:]
        with pytest.raises(HomeAssistantError, match="not verified"):
            await store.async_reset_defaults()
        assert client.resets == 0
        client.cfg = SAMPLE
        get_shared(hass).flashing = True
        with raises_key("flash_running"):
            await store.async_reset_defaults()
        with raises_key("flash_running"):
            await store.async_set_pin(1)
        assert client.resets == 0 and client.pin_writes == 0
    finally:
        get_shared(hass).flashing = False
        [c.stop() for c in ctx]


async def test_hw_ver_is_read_only_diagnostic(hass: HomeAssistant, enable_bluetooth, fast_batch):
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        eid = ent(hass, "sensor", "hw_ver")
        assert er.async_get(hass).async_get(eid).entity_category == "diagnostic"
        await enable(hass, eid)
        assert hass.states.get(eid).state == "10"
        assert "hw_ver" not in cfg.FIELDS  # no setter exists
    finally:
        [c.stop() for c in ctx]


def beta_sw(hass):
    return er.async_get(hass).async_get_entity_id("switch", DOMAIN, f"{MAC}_beta_firmware")


GATED = [
    ("number", "adv_interval"),
    ("number", "measure_interval"),
    ("select", "rf_tx_power"),
    ("select", "smiley"),
    ("switch", "screen_off"),
    ("button", "restore_config"),
    ("button", "reset_defaults"),
    ("sensor", "hw_ver"),
]


class Refusing(FakeClient):
    """Every attempt refused (a PIN, or pairing that does not give an authenticated link)."""

    async def pair(self):
        self.pair_calls += 1
        return True  # "succeeds", but the link stays unauthenticated

    async def start_notify(self, char, cb):
        raise PermissionError("Bluetooth GATT Error handle=45 error=5 description=Insufficient authentication")


async def test_pin_refusal_in_the_background_read_raises_the_repair_issue(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    """Setup makes no connection. If the first read is refused (a PIN set after the entry was created) the settings
    entities stay unavailable, no retry loop starts, and the existing "PIN set" repair issue explains it."""
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.helpers import issue_registry as ir

    state = {"client": Refusing(cfg=SAMPLE)}
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=MAC, title="ATC_000001", data={CONF_ADDRESS: MAC, CONF_VERSION: 0x59, CONF_HW_ID: 10}
    )
    entry.add_to_hass(hass)

    @asynccontextmanager
    async def fake(hass_, address, name):
        yield state["client"]

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
        store = entry.runtime_data
        await store.first_read
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED  # no ConfigEntryError any more
        assert ir.async_get(hass).async_get_issue(DOMAIN, f"pin_set_{entry.entry_id}")
        assert hass.states.get(ent(hass, "number", "adv_interval")).state == "unavailable"
        # the PIN is gone: the Refresh button reads it
        state["client"] = FakeClient(cfg=SAMPLE)
        await hass.services.async_call(
            "button", "press", {"entity_id": ent(hass, "button", "refresh_config")}, blocking=True
        )
        assert hass.states.get(ent(hass, "number", "adv_interval")).state == "1.0"
    finally:
        [c.stop() for c in ctx]


async def test_later_read_failure_keeps_entities_but_unavailable(hass: HomeAssistant, enable_bluetooth, fast_batch):
    """Setup authenticated once; if a later read fails the entities stay (unavailable until a read works)."""
    state = {"client": FakeClient(cfg=SAMPLE)}
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=MAC, title="ATC_000001", data={CONF_ADDRESS: MAC, CONF_VERSION: 0x59, CONF_HW_ID: 10}
    )
    entry.add_to_hass(hass)

    @asynccontextmanager
    async def fake(hass_, address, name):
        yield state["client"]

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
        await hass.async_block_till_done()
        assert hass.states.get(ent(hass, "number", "adv_interval")).state == "1.0"
        state["client"] = Refusing(cfg=SAMPLE)
        refresh = ent(hass, "button", "refresh_config")
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call("button", "press", {"entity_id": refresh}, blocking=True)
        # the last good block is still shown; writes would re-read first and fail cleanly
        assert hass.states.get(ent(hass, "number", "adv_interval")).state == "1.0"
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "number", "set_value", {"entity_id": ent(hass, "number", "adv_interval"), "value": 2}, blocking=True
            )
    finally:
        [c.stop() for c in ctx]


async def test_unverified_firmware_gets_no_config_entities_buttons_or_restore(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    old = bytes([0x55, 0x56]) + SAMPLE[2:]  # 5.6: a version nobody compared
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=old), version=0x56)
    try:
        for dom, key in GATED:
            assert ent(hass, dom, key) is None, (dom, key)
        assert ent(hass, "button", "refresh_config") and hass.states.get("update.atc_000001")
        assert hass.states.get(beta_sw(hass))
    finally:
        [c.stop() for c in ctx]


async def test_short_entity_names_and_warning_attributes(hass: HomeAssistant, enable_bluetooth, fast_batch):
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        reg = er.async_get(hass)
        for dom, key in [("select", "adv_type"), ("switch", "longrange"), ("button", "reset_defaults")]:
            e = reg.async_get(ent(hass, dom, key))
            assert len(e.entity_id) < 50 and "(" not in (e.name or e.original_name or ""), e.entity_id
        await enable(hass, ent(hass, "select", "adv_type"))
        assert "BTHome" in hass.states.get(ent(hass, "select", "adv_type")).attributes["warning"]
    finally:
        [c.stop() for c in ctx]


# -- reflash button ---------------------------------------------------------------------
async def test_reflash_button_flashes_even_when_up_to_date(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE[:11] + bytes([0, 0]))  # hardware id 0 is in the test catalog; 5.9 == latest 5.9
    seen = {}
    orig = client.write_gatt_char

    async def spy(char, data, response):
        if char.endswith("2b12"):
            seen.setdefault("ota", []).append(get_shared(hass).flashing)
        return await orig(char, data, response)

    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        assert hass.states.get("update.atc_000001").state == "off"  # up to date: Install would refuse
        eid = ent(hass, "button", "reflash_firmware")
        reg = er.async_get(hass).async_get(eid)
        assert reg.entity_category == "config" and reg.disabled_by is None
        assert hass.states.get(eid).state != "unavailable"
        assert "1.5 to 3 minutes" in hass.states.get(eid).attributes["warning"]
        client.write_gatt_char = spy
        with patch("custom_components.pvvx_ota.update.async_call_later"):
            await hass.services.async_call("button", "press", {"entity_id": eid}, blocking=True)
        assert seen["ota"] and all(seen["ota"])  # blocks went out while config writes were refused
        assert entry.data["cfg_snapshot"]["fields"]["adv_interval"] == 16  # snapshot before the flash, as with Install
        assert get_shared(hass).flashing is False
    finally:
        [c.stop() for c in ctx]


async def test_reflash_button_unavailable_while_flashing(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        eid = ent(hass, "button", "reflash_firmware")
        store = entry.runtime_data
        get_shared(hass).flashing = True
        store.async_notify()
        await hass.async_block_till_done()
        assert hass.states.get(eid).state == "unavailable"
        try:  # an unavailable entity is skipped (or refused) by HA; either way nothing is flashed
            await hass.services.async_call("button", "press", {"entity_id": eid}, blocking=True)
        except HomeAssistantError:
            pass
        assert client.writes == []
        get_shared(hass).flashing = False
        store.async_notify()
        await hass.async_block_till_done()
        assert hass.states.get(eid).state != "unavailable"
        # the shared lock held by another operation also makes it unavailable
        async with get_shared(hass).lock:
            store.async_notify()
            await hass.async_block_till_done()
            assert hass.states.get(eid).state == "unavailable"
    finally:
        get_shared(hass).flashing = False
        [c.stop() for c in ctx]


async def test_post_flash_check_really_runs_and_updates_the_version(hass: HomeAssistant, enable_bluetooth, fast_batch):
    """Regression: the scheduled post-flash read used a lambda that ran in an executor thread and never started."""
    hw0 = SAMPLE[:11] + bytes([0, 0])
    newer = bytes([0x55, 0x60]) + hw0[2:]
    clients = {"n": 0}

    @asynccontextmanager
    async def fake(hass_, address, name):
        clients["n"] += 1
        # setup read and the flash itself see 5.9; the read after the flash sees the new 6.0
        yield FakeClient(cfg=hw0 if clients["n"] <= 2 else newer)

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=MAC, title="ATC_000001", data={CONF_ADDRESS: MAC, CONF_VERSION: 0x59, CONF_HW_ID: 0}
    )
    entry.add_to_hass(hass)
    p1, p2 = patch_catalog()
    with (
        p1,
        p2,
        patch("custom_components.pvvx_ota.update.async_connected", fake),
        patch("custom_components.pvvx_ota.store.async_connected", fake),
        patch("custom_components.pvvx_ota.update.POST_FLASH_DELAY", 0.05),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await hass.services.async_call(DOMAIN, "flash_latest", {"entity_id": "update.atc_000001"}, blocking=True)
        for _ in range(100):  # the check runs as a background task shortly after the delay
            await asyncio.sleep(0.05)
            if hass.states.get("update.atc_000001").attributes["installed_version"] == "6.0":
                break
        assert hass.states.get("update.atc_000001").attributes["installed_version"] == "6.0"


# -- after a flash: release the link, open no other connection while the sensor reboots -------------
async def test_flash_releases_the_link_and_the_refresh_after_it_stays_quiet(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "homeassistant", {})
    client = FakeClient(cfg=SAMPLE[:11] + bytes([0, 0]))
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        with patch("custom_components.pvvx_ota.update.async_call_later"):
            await hass.services.async_call(DOMAIN, "flash_latest", {"entity_id": "update.atc_000001"}, blocking=True)
            assert client.disconnected is True  # dropped right after the end marker, not left open
            n = len(calls)
            # HA's own refresh right after the service must not connect to the rebooting sensor
            await hass.services.async_call(
                "homeassistant", "update_entity", {"entity_id": "update.atc_000001"}, blocking=True
            )
            await hass.async_block_till_done()
            assert len(calls) == n  # no new connection during the quiet period
    finally:
        [c.stop() for c in ctx]


async def test_disconnect_that_never_confirms_is_bounded(monkeypatch, caplog):
    from custom_components.pvvx_ota import device

    monkeypatch.setattr(device, "DISCONNECT_TIMEOUT", 0.05)

    class Stuck:
        async def disconnect(self):
            await asyncio.sleep(3600)

    await device.disconnect_quietly(Stuck(), "X")
    assert "did not confirm the disconnect" in caplog.text


# -- reboot button -------------------------------------------------------------------------
async def test_reboot_button_sends_0x72_and_drops_the_link(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        eid = ent(hass, "button", "reboot_sensor")
        reg = er.async_get(hass).async_get(eid)
        assert reg.entity_category == "config" and reg.disabled_by is None and reg.original_device_class == "restart"
        assert hass.states.get(eid).state != "unavailable"
        with patch("custom_components.pvvx_ota.store.REBOOT_WATCH", 0.0):
            await hass.services.async_call("button", "press", {"entity_id": eid}, blocking=True)
        await hass.async_block_till_done()
        assert client.reboots == 1 and client.disconnected is True  # the link drop is what reboots the sensor
        assert client.cfg_writes == []  # settings untouched
    finally:
        [c.stop() for c in ctx]


async def test_reboot_without_reply_is_a_failure(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)
    client.no_reboot_reply = True
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        with (
            patch("custom_components.pvvx_ota.store.REBOOT_REPLY_TIMEOUT", 0.05),
            patch("custom_components.pvvx_ota.store.REBOOT_WATCH", 0.0),
        ):
            with raises_key("reboot_not_confirmed"):
                store = entry.runtime_data
                await store.async_reboot()
        assert client.reboots == 1
    finally:
        [c.stop() for c in ctx]


async def test_reboot_unavailable_and_refused_while_flashing(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        eid = ent(hass, "button", "reboot_sensor")
        store = entry.runtime_data
        get_shared(hass).flashing = True
        store.async_notify()
        await hass.async_block_till_done()
        assert hass.states.get(eid).state == "unavailable"
        with raises_key("flash_running"):
            await store.async_reboot()
        assert client.reboots == 0
    finally:
        get_shared(hass).flashing = False
        [c.stop() for c in ctx]


async def test_reboot_asks_for_pairing_like_every_command(hass: HomeAssistant, enable_bluetooth, fast_batch):
    """A PIN sensor never got this far (no entry), but a refused read must not send the reboot."""
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        client.needs_pairing = True  # the sensor starts refusing
        store = entry.runtime_data
        with pytest.raises(HomeAssistantError):
            await store.async_reboot()
        assert client.reboots == 0
    finally:
        [c.stop() for c in ctx]


# -- encryption comes back after a flash ------------------------------------------------------
async def post_flash(hass, crypto_before: bool, has_key: bool, differ: bool = False, caplog=None):
    """Flash AC:CC:22-like sensor whose block after the flash has the encryption flag cleared (and maybe other settings)."""
    hw0 = SAMPLE[:11] + bytes([0, 0])
    before = bytearray(hw0)
    if crypto_before:
        before[3] |= 0x08  # flg2 bit 3 = adv_crypto
    after = bytearray(before)
    after[3] &= ~0x08 & 0xFF  # the flash clears the flag
    if differ:
        after[6] = 0x08  # advertising interval back to 0.5 s
    state = {"cfg": bytes(before), "n": 0, "bindkey": bytes([7] * 16) if has_key else bytes(16), "after": bytes(after)}
    clients: list[FakeClient] = []

    @asynccontextmanager
    async def fake(hass_, address, name):
        state["n"] += 1
        c = FakeClient(cfg=state["cfg"])
        c.bindkey = state["bindkey"]
        clients.append(c)
        try:
            yield c
        finally:
            state["cfg"] = c.cfg
            if state["n"] == 2:  # the flash connection ends: from now on the sensor reports the post-flash block
                state["cfg"] = state["after"]

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=MAC, title="ATC_000001", data={CONF_ADDRESS: MAC, CONF_VERSION: 0x59, CONF_HW_ID: 0}
    )
    entry.add_to_hass(hass)
    p1, p2 = patch_catalog()
    with (
        p1,
        p2,
        patch("custom_components.pvvx_ota.update.async_connected", fake),
        patch("custom_components.pvvx_ota.store.async_connected", fake),
        patch("custom_components.pvvx_ota.update.POST_FLASH_DELAY", 0.05),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await hass.services.async_call(DOMAIN, "flash_latest", {"entity_id": "update.atc_000001"}, blocking=True)
        for _ in range(100):  # the post-flash check and what follows run as a background task
            await asyncio.sleep(0.05)
            if state["n"] >= 3:
                break
        await asyncio.sleep(0.5)
        await hass.async_block_till_done()
    return entry, state, clients


def issue(hass, entry, key):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{key}_{entry.entry_id}")


async def test_encryption_restored_after_flash_without_a_new_key(
    hass: HomeAssistant, enable_bluetooth, fast_batch, caplog
):
    caplog.set_level("DEBUG")
    entry, state, clients = await post_flash(hass, crypto_before=True, has_key=True)
    assert cfg.get(state["cfg"], "adv_crypto") == 1  # switched back on
    assert sum(c.key_writes for c in clients) == 0  # no key was written: it stayed on the sensor
    assert state["bindkey"] == bytes([7] * 16) and bytes([7] * 16).hex() not in caplog.text
    assert issue(hass, entry, "encryption_restore_failed") is None
    assert "BTHome encryption switched back on" in caplog.text
    store = entry.runtime_data
    assert cfg.get(store.raw, "adv_crypto") == 1


async def test_encryption_that_was_off_is_left_alone(hass: HomeAssistant, enable_bluetooth, fast_batch):
    entry, state, clients = await post_flash(hass, crypto_before=False, has_key=True)
    assert cfg.get(state["cfg"], "adv_crypto") == 0
    assert all(c.cfg_writes == [] for c in clients) and sum(c.key_writes for c in clients) == 0
    assert issue(hass, entry, "encryption_restore_failed") is None


async def test_failed_restore_raises_a_repair_issue_without_leaking(
    hass: HomeAssistant, enable_bluetooth, fast_batch, caplog
):
    caplog.set_level("DEBUG")
    entry, state, clients = await post_flash(hass, crypto_before=True, has_key=False)  # the sensor lost its key
    assert cfg.get(state["cfg"], "adv_crypto") == 0  # never switched on without a key
    assert issue(hass, entry, "encryption_restore_failed") is not None
    assert "holds no encryption key" in caplog.text
    assert sum(c.key_writes for c in clients) == 0


async def test_other_settings_are_only_flagged_not_reapplied(hass: HomeAssistant, enable_bluetooth, fast_batch):
    entry, state, clients = await post_flash(hass, crypto_before=False, has_key=True, differ=True)
    assert state["cfg"][6] == 0x08  # advertising interval was NOT written back automatically
    assert issue(hass, entry, "settings_differ_after_flash") is not None


# -- diagnostics ---------------------------------------------------------------------------------
async def test_diagnostics_content_and_redaction(hass: HomeAssistant, enable_bluetooth, fast_batch):
    import json

    from custom_components.pvvx_ota.diagnostics import async_get_config_entry_diagnostics

    client = FakeClient(cfg=SAMPLE[:11] + bytes([0, 0]))
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        # secrets that must never be shown, wherever they might end up in entry data/options
        secret_key, secret_pin = "00112233445566778899aabbccddeeff", "424242"
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "bindkey": secret_key}, options={**entry.options, "pin": secret_pin}
        )
        with patch("custom_components.pvvx_ota.update.async_call_later"):
            await hass.services.async_call(DOMAIN, "flash_latest", {"entity_id": "update.atc_000001"}, blocking=True)
        res = await hass.services.async_call(
            DOMAIN,
            "set_encryption",
            {"entity_id": "update.atc_000001", "enable": True},
            blocking=True,
            return_response=True,
        )
        key_from_action = res["update.atc_000001"]["bindkey"]
        await hass.services.async_call(
            DOMAIN, "set_pin", {"entity_id": "update.atc_000001", "pin": 135790, "confirm": True}, blocking=True
        )
        diag = await async_get_config_entry_diagnostics(hass, entry)
        text = json.dumps(diag)
        assert (
            secret_key not in text and secret_pin not in text and key_from_action not in text and "135790" not in text
        )
        assert diag["entry"]["data"]["bindkey"] == "**REDACTED**" and diag["entry"]["options"]["pin"] == "**REDACTED**"
        assert diag["config_block"]["layout_verified"] and diag["config_block"]["firmware_version"] == "5.9"
        assert "adv_interval" in diag["config_block"]["fields"]
        assert set(diag["firmware_catalog"]) >= {
            "loaded",
            "stable_version",
            "beta_version",
            "images",
            "age_s",
        }  # catalog fetch is patched in tests
        assert diag["last_flash"]["result"] == "ok" and diag["last_flash"]["blocks"] > 0
        assert diag["last_flash"]["blocks_per_s"] > 0
        assert diag["devices"] and diag["devices"][0]["connections"]
        assert any(e.startswith("update.") for e in diag["entities"])
    finally:
        [c.stop() for c in ctx]


async def test_diagnostics_after_failed_flash_records_the_error(hass: HomeAssistant, enable_bluetooth, fast_batch):
    from custom_components.pvvx_ota.diagnostics import async_get_config_entry_diagnostics

    client = FakeClient(cfg=SAMPLE[:11] + bytes([0, 0]), fail_status_at=3, status_code=2)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0)
    try:
        with patch("custom_components.pvvx_ota.update.async_call_later"), pytest.raises(HomeAssistantError):
            await hass.services.async_call(DOMAIN, "flash_latest", {"entity_id": "update.atc_000001"}, blocking=True)
        diag = await async_get_config_entry_diagnostics(hass, entry)
        assert diag["last_flash"]["result"].startswith("failed")
    finally:
        [c.stop() for c in ctx]


# -- translations ----------------------------------------------------------------------------------
def test_german_translation_is_complete_and_entities_use_translation_keys():
    import json
    from pathlib import Path

    base = Path("custom_components/pvvx_ota")

    def keys(x, pre=""):
        out = set()
        for k, v in x.items():
            out |= keys(v, pre + k + ".") if isinstance(v, dict) else {pre + k}
        return out

    strings = json.loads((base / "strings.json").read_text())
    en = json.loads((base / "translations/en.json").read_text())
    de = json.loads((base / "translations/de.json").read_text())
    assert strings == en and keys(en) == keys(de)
    # placeholders survive the translation
    import re

    for k in keys(en):
        a, b = en, de
        for part in k.split("."):
            a, b = a[part], b[part]
        assert set(re.findall(r"\{\w+\}", a)) == set(re.findall(r"\{\w+\}", b)), k
    assert de["entity"]["button"]["reboot_sensor"]["name"] == "Sensor neu starten"


async def test_entity_names_come_from_the_translations(hass: HomeAssistant, enable_bluetooth, fast_batch):
    entry, calls, ctx = await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    try:
        reg = er.async_get(hass)
        assert reg.async_get(ent(hass, "button", "reboot_sensor")).original_name == "Reboot sensor"
        assert reg.async_get(ent(hass, "number", "adv_interval")).translation_key == "adv_interval"
        assert reg.async_get(ent(hass, "switch", "beta_firmware")).original_name == "Beta firmware"
    finally:
        [c.stop() for c in ctx]


# -- firmware 5.5 (docs/version-layouts.md) ---------------------------------------------
# Block shaped like a CGG1-M 2022 (hw 7) on V5.5: flg 0x03 = BTHome, flg2 0x00, flg3 0x04, event_adv_cnt 6,
# adv 16 (1 s), meas 4, rf 169, latency 0x13, step 0x37, hw_ver 7, averaging 0
V55_CGG1 = bytes([0x55, 0x55, 0x03, 0x00, 0x04, 0x06, 0x10, 0x04, 0xA9, 0x13, 0x37, 0x07, 0x00])


def test_v55_is_verified_with_the_same_capabilities_as_v59_for_bad_hardware():
    assert cfg.is_verified(V55_CGG1)
    v59 = bytes([0x55, 0x59]) + V55_CGG1[2:]
    for hw in (0, 7, 9, 12, 49, None):
        assert cfg.capabilities(V55_CGG1, hw) == cfg.capabilities(v59, hw)
    assert cfg.capabilities(V55_CGG1, 7) == GENERIC | {"temp_f", "screen_off"}


def test_v55_read_modify_write_changes_only_the_target_bits():
    out = cfg.apply(V55_CGG1, {"temp_f": 1, "screen_off": 1, "adv_interval": 32}, hw_id=7)
    assert payload_diff(V55_CGG1, out) == {2: (0x03, 0x13), 3: (0x00, 0x80), 6: (0x10, 32)}
    assert len(out) == len(V55_CGG1) - 2 and out[2] == 0x04  # flg3 untouched
    assert cfg.hw_ver(bytes([0x55, 0x55]) + out) == 7  # hw_ver byte untouched
    with pytest.raises(cfg.ConfigError, match="cannot be changed"):
        cfg.apply(V55_CGG1, {"smiley": 1}, hw_id=7)  # glyph sets differ on e-ink, not offered


async def test_v55_sensor_gets_entities_and_snapshot(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=V55_CGG1)
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=7, version=0x55)
    try:
        assert ent(hass, "number", "adv_interval") is not None and ent(hass, "button", "restore_config") is not None
        store = entry.runtime_data
        store.take_snapshot()
        snap = entry.data["cfg_snapshot"]["fields"]
        assert snap["adv_interval"] == 16 and snap["temp_f"] == 0 and "adv_crypto" not in snap
        assert client.cfg_writes == []  # nothing was written to the sensor
    finally:
        [c.stop() for c in ctx]


# -- firmware versions 4.2 .. 5.4 (docs/version-layouts.md, "Older versions") ------------------------
def v_block(ver: int, hw: int = 0x0A, b4: int = 0x04, b5: int = 0x06) -> bytes:
    """Block of that firmware version: bytes 4-5 are offsets (<= 4.6), reserved (4.7-5.0) or flg3/event_adv_cnt (>= 5.1)."""
    return bytes([0x55, ver, 0xA3, 0x05, b4, b5, 0x10, 0x04, 0xA9, 0x13, 0x37, hw, 0x00])


NEW_VERIFIED = [0x42, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49, 0x50, 0x51, 0x52, 0x53, 0x54, 0x55, 0x57, 0x58, 0x59]


@pytest.mark.parametrize("ver", NEW_VERIFIED)
def test_verified_versions(ver):
    assert cfg.is_verified(v_block(ver))


@pytest.mark.parametrize("ver", [0x11, 0x14, 0x21, 0x26, 0x37, 0x39, 0x40, 0x41, 0x56, 0x5A])
def test_older_and_unknown_versions_stay_refused(ver):
    """4.1 and older: flg2 bits 5-7 differ (longrange on bit 7) and the reply can be stale; 5.6 was never compared."""
    raw = v_block(ver)
    assert not cfg.is_verified(raw) and cfg.capabilities(raw, 10) == set()
    with pytest.raises(cfg.ConfigError, match="not verified"):
        cfg.apply(raw, {"adv_interval": 20}, 10)


@pytest.mark.parametrize(
    ("ver", "hw", "missing"),
    [
        (0x59, 12, set()),
        (0x54, 12, set()),
        (0x53, 12, {"date_ddmm"}),  # 5.1-5.3: flg3 bit 6 reserved
        (0x51, 12, {"date_ddmm"}),
        (0x50, 12, {"date_ddmm"}),  # 4.9/5.0: weekday mask 0x80 only
        (0x49, 12, {"date_ddmm"}),
        (0x49, 9, {"date_ddmm"}),  # (hw 9 never had it)
        (0x48, 9, {"date_ddmm", "not_day_of_week"}),  # 4.8: flg3 unused
        (0x47, 9, {"date_ddmm", "not_day_of_week"}),
        (0x43, 9, {"date_ddmm", "not_day_of_week"}),
        (0x42, 9, {"date_ddmm", "not_day_of_week", "screen_off"}),  # 4.2: flg2 bit 7 reserved
        (0x42, 0, {"screen_off"}),
        (0x42, 7, {"screen_off"}),
        (0x46, 0, set()),
    ],
)
def test_capabilities_per_version(ver, hw, missing):
    full = cfg.capabilities(v_block(0x59), hw)
    got = cfg.capabilities(v_block(ver), hw)
    assert got == full - missing
    if hw in (9, 12):
        assert {
            "x100",
            "screen_type",
            "time_am_pm",
            "temp_f",
        } <= got  # same bits since 4.2 (MJWSD05MMC appeared in 4.2)


@pytest.mark.parametrize("ver", [0x42, 0x44, 0x46])  # offsets in the block
def test_old_block_with_offsets_is_written_back_unchanged(ver):
    raw = v_block(ver, b4=0xFB, b5=0x07)  # temp offset -0.5, humi offset +0.7: never touched
    out = cfg.apply(raw, {"adv_interval": 32, "temp_f": 1}, hw_id=0)
    assert payload_diff(raw, out) == {2: (0xA3, 0xB3), 6: (0x10, 32)}
    assert out[2:4] == bytes([0xFB, 0x07]) and cfg.hw_ver(bytes([0x55, ver]) + out) == 0x0A
    assert "screen_off" not in cfg.capabilities(raw, 0) or ver >= 0x43


@pytest.mark.parametrize("ver", [0x47, 0x48, 0x50])  # reserved bytes 4-5
def test_reserved_bytes_survive(ver):
    raw = v_block(ver, b4=0x81, b5=0x42)
    out = cfg.apply(raw, {"comfort_smiley": 1}, hw_id=10)
    assert payload_diff(raw, out) == {2: (0xA3, 0xA7)}


def test_snapshot_skips_fields_the_version_lacks():
    snap = cfg.snapshot(v_block(0x42, hw=9), 9)
    assert "screen_off" not in snap and "x100" in snap and "adv_interval" in snap
    assert "screen_off" in cfg.snapshot(v_block(0x45, hw=9), 9)


async def test_v46_sensor_gets_entities_snapshot_and_no_calibration_read(
    hass: HomeAssistant, enable_bluetooth, fast_batch
):
    client = FakeClient(cfg=v_block(0x46, hw=0))
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=0, version=0x46)
    try:
        assert ent(hass, "number", "adv_interval") is not None and ent(hass, "button", "restore_config") is not None
        assert ent(hass, "number", "temp_offset") is None  # no 0x25 before 4.8
        store = entry.runtime_data
        assert store.calib is None and client.coef_writes == []
        store.take_snapshot()
        assert entry.data["cfg_snapshot"]["fields"]["adv_interval"] == 16
        assert client.cfg_writes == []
    finally:
        [c.stop() for c in ctx]


def test_capabilities_from_the_stored_version_match_the_ones_of_a_block():
    """What the entry stores (version byte, hardware id) decides the entities before any read: the same answer as a block's."""
    for version in sorted(cfg.VERIFIED_VERSIONS):
        for hw in (None, 0, 9, 10, 12):
            block = bytes([0x55, version]) + bytes(cfg.MIN_LEN - 2)
            assert cfg.capabilities_for(version, hw) == cfg.capabilities(block, hw)
    assert cfg.capabilities_for(0x41, 10) == set() and cfg.capabilities_for(None, 10) == set()  # layout not verified


async def test_switching_encryption_on_clears_a_failed_restore(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        entry.runtime_data._issue("encryption_restore_failed")
        await hass.services.async_call(
            DOMAIN,
            "set_encryption",
            {"entity_id": UPDATE, "enable": True, "key": KEY},
            blocking=True,
            return_response=True,
        )
        assert issue(hass, entry, "encryption_restore_failed") is None
    finally:
        [c.stop() for c in ctx]
