"""Select options are stable lowercase ids; the shown texts come from the translations."""

import json
import re
from pathlib import Path

from homeassistant.core import HomeAssistant

from custom_components.pvvx_ota import config as cfg
from custom_components.pvvx_ota import const

from .fake_gatt import FakeClient
from .test_config import MJ_SAMPLE, SAMPLE, enable, ent, fast_batch, setup_cfg_entry  # noqa: F401 - fixture

PKG = Path(const.__file__).parent
KEY_RE = re.compile(r"^[a-z0-9_]+$")
OPTIONS = {
    "adv_type": cfg.ADV_TYPES,
    "temp_unit_display": cfg.TEMP_UNITS,
    "smiley": cfg.SMILEYS,
    "screen_type": cfg.SCREEN_TYPES,
    "rf_tx_power": [cfg.tx_power_id(r) for r in cfg.TX_POWER],
}


def states(path: str) -> dict[str, dict[str, str]]:
    return {k: v["state"] for k, v in json.loads((PKG / path).read_text())["entity"]["select"].items()}


def test_every_option_has_a_translation_in_every_language():
    for path in ("strings.json", "translations/en.json", "translations/de.json"):
        texts = states(path)
        assert set(texts) == set(OPTIONS), path
        for key, ids in OPTIONS.items():
            assert set(texts[key]) == set(ids), (path, key)
            assert all(texts[key].values())
    assert states("strings.json") == states("translations/en.json")


def test_option_ids_are_valid_translation_keys():
    for ids in OPTIONS.values():
        assert all(KEY_RE.match(i) for i in ids), ids
        assert len(set(ids)) == len(ids)
    assert cfg.raw_id(99) == "raw_99" and KEY_RE.match(cfg.raw_id(99))


def test_tx_power_ids_are_derived_from_dbm_and_unique():
    ids = {r: cfg.tx_power_id(r) for r in cfg.TX_POWER}
    assert len(set(ids.values())) == len(cfg.TX_POWER)
    assert ids[191] == "dbm_p3_01" and ids[169] == "dbm_p0_04" and ids[156] == "dbm_m3_03" and ids[63] == "dbm_p10_46"
    for path in ("strings.json", "translations/en.json", "translations/de.json"):
        for raw, i in ids.items():
            assert states(path)["rf_tx_power"][i] == f"{cfg.TX_POWER[raw]:+.2f} dBm"


async def test_select_option_ids_write_the_raw_values(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE)
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        for key in ("rf_tx_power", "temp_unit_display", "smiley", "adv_type"):
            await enable(hass, ent(hass, "select", key))

        async def pick(key, option):
            await hass.services.async_call(
                "select", "select_option", {"entity_id": ent(hass, "select", key), "option": option}, blocking=True
            )

        await pick("rf_tx_power", "dbm_m3_03")
        assert cfg.get(client.cfg, "rf_tx_power") == 156
        await pick("temp_unit_display", "fahrenheit")
        assert cfg.get(client.cfg, "temp_f") == 1
        await pick("smiley", "frame_sad")
        assert cfg.get(client.cfg, "smiley") == 6
        await pick("adv_type", "mi")
        assert cfg.get(client.cfg, "adv_type") == 2
        assert hass.states.get(ent(hass, "select", "rf_tx_power")).state == "dbm_m3_03"
    finally:
        [c.stop() for c in ctx]


async def test_unlisted_tx_power_shows_as_raw_id(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=SAMPLE[:8] + bytes([0x7F]) + SAMPLE[9:])
    entry, calls, ctx = await setup_cfg_entry(hass, client)
    try:
        await enable(hass, ent(hass, "select", "rf_tx_power"))
        state = hass.states.get(ent(hass, "select", "rf_tx_power"))
        assert state.state == "raw_127" and state.attributes["options"][-1] == "raw_127"
    finally:
        [c.stop() for c in ctx]


async def test_unlisted_screen_type_shows_as_raw_id(hass: HomeAssistant, enable_bluetooth, fast_batch):
    client = FakeClient(cfg=MJ_SAMPLE[:3] + bytes([(MJ_SAMPLE[3] & ~7) | 6]) + MJ_SAMPLE[4:])
    entry, calls, ctx = await setup_cfg_entry(hass, client, hw=9)
    try:
        await enable(hass, ent(hass, "select", "screen_type"))
        state = hass.states.get(ent(hass, "select", "screen_type"))
        assert state.state == "raw_6" and state.attributes["options"][-1] == "raw_6"
        assert "external" not in state.attributes["options"]
    finally:
        [c.stop() for c in ctx]
