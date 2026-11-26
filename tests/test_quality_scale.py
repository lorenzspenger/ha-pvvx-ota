"""What the quality scale file claims, checked against the files and entities it talks about."""

import json
from pathlib import Path

from homeassistant.components.button import ButtonDeviceClass
from homeassistant.components.number import NumberDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.pvvx_ota import button

from .fake_gatt import FakeClient
from .test_config import MAC, SAMPLE, fast_batch, setup_cfg_entry  # noqa: F401

PKG = Path(button.__file__).parent
RULES = {
    "bronze": "action-setup appropriate-polling brands common-modules config-flow config-flow-test-coverage dependency-transparency docs-actions docs-conditions docs-high-level-description docs-installation-instructions docs-removal-instructions docs-triggers entity-event-setup entity-unique-id has-entity-name runtime-data test-before-configure test-before-setup unique-config-entry",
    "silver": "action-exceptions config-entry-unloading docs-configuration-parameters docs-installation-parameters entity-unavailable integration-owner log-when-unavailable parallel-updates reauthentication-flow test-coverage",
    "gold": "devices diagnostics discovery discovery-update-info docs-data-update docs-examples docs-known-limitations docs-supported-devices docs-supported-functions docs-troubleshooting docs-use-cases dynamic-devices entity-category entity-device-class entity-disabled-by-default entity-translations exception-translations icon-translations reconfiguration-flow repair-issues stale-devices",
    "platinum": "async-dependency inject-websession strict-typing",
}


def parse_rules() -> dict[str, tuple[str, str]]:
    """The file is simple enough to read without a YAML library: ``rule:`` then ``status:`` and an optional ``comment:``."""
    rules: dict[str, tuple[str, str]] = {}
    name = None
    for line in (PKG / "quality_scale.yaml").read_text().splitlines():
        if (
            line.startswith("  ")
            and not line.startswith("    ")
            and line.strip().endswith(":")
            and not line.strip().startswith("#")
        ):
            name = line.strip()[:-1]
            rules[name] = ("", "")
        elif name and line.strip().startswith("status:"):
            rules[name] = (line.split(":", 1)[1].strip(), rules[name][1])
        elif name and line.strip().startswith("comment:"):
            rules[name] = (rules[name][0], line.split(":", 1)[1].strip())
    return rules


def test_every_rule_of_the_scale_is_listed_with_a_valid_status_and_exemptions_say_why():
    rules = parse_rules()
    expected = {rule for tier in RULES.values() for rule in tier.split()}
    assert set(rules) == expected
    for rule, (status, comment) in rules.items():
        assert status in ("done", "exempt", "todo"), rule
        if status == "exempt":
            assert len(comment) > 40, f"{rule}: an exemption needs a real reason"


def test_every_rule_up_to_gold_is_done_or_exempt():
    rules = parse_rules()
    for tier in ("bronze", "silver", "gold"):
        for rule in RULES[tier].split():
            assert rules[rule][0] in ("done", "exempt"), rule


def test_every_entity_and_action_has_an_icon_except_those_the_device_class_covers():
    strings = json.loads((PKG / "strings.json").read_text())
    icons = json.loads((PKG / "icons.json").read_text())
    covered_by_device_class = {("button", "reboot_sensor")}
    for platform, keys in strings["entity"].items():
        for key in keys:
            if (platform, key) not in covered_by_device_class:
                assert key in icons["entity"][platform], (platform, key)
    assert {p: set(v) for p, v in icons["entity"].items()} == {
        p: set(v) - {k for (pl, k) in covered_by_device_class if pl == p} for p, v in strings["entity"].items()
    }
    assert set(icons["services"]) == set(strings["services"])
    assert all(v["service"].startswith("mdi:") for v in icons["services"].values())


def test_no_icon_is_set_in_code():
    for path in PKG.glob("*.py"):
        assert "_attr_icon" not in path.read_text(), path.name


async def test_device_classes_where_one_fits(hass: HomeAssistant, enable_bluetooth):
    await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))
    for domain, key, device_class in (
        ("button", "reflash_firmware", ButtonDeviceClass.UPDATE),
        ("button", "reboot_sensor", ButtonDeviceClass.RESTART),
        ("number", "adv_interval", NumberDeviceClass.DURATION),
    ):
        entity_id = er.async_get(hass).async_get_entity_id(domain, "pvvx_ota", f"{MAC}_{key}")
        assert hass.states.get(entity_id).attributes["device_class"] == device_class.value, key
    assert hass.states.get("update.atc_000001").attributes["device_class"] == "firmware"


async def test_a_device_of_our_own_carries_the_firmware_version(hass: HomeAssistant, enable_bluetooth):
    await setup_cfg_entry(hass, FakeClient(cfg=SAMPLE))  # no BTHome device: the entities create their own
    entity = er.async_get(hass).async_get("update.atc_000001")
    device = dr.async_get(hass).async_get(entity.device_id)
    assert device.manufacturer == "pvvx" and device.sw_version == "5.9"
    assert (dr.CONNECTION_BLUETOOTH, MAC) in device.connections
