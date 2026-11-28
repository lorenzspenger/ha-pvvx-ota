"""Every translated exception: the key exists in strings.json and both languages, and the call passes what the text uses."""

import ast
import json
import re
from pathlib import Path

from custom_components.pvvx_ota import const  # noqa: E402 - also puts the package on the path

PKG = Path(const.__file__).parent
CALLS = {"ha_error", "validation_error", "not_ready"}
SPECIAL = {"pair_connect_failed": {"proxy", "address"}}  # PairConnectError builds its own placeholders


def used() -> dict[str, list[set[str]]]:
    found: dict[str, list[set[str]]] = {}
    for path in PKG.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) in CALLS and node.args):
                continue
            first = node.args[0]
            keys = [first.value] if isinstance(first, ast.Constant) else [b.value for b in (first.body, first.orelse)]
            for key in keys:
                found.setdefault(key, []).append({kw.arg for kw in node.keywords if kw.arg})
    return found


def messages(path: str) -> dict[str, str]:
    return {k: v["message"] for k, v in json.loads((PKG / path).read_text())["exceptions"].items()}


def test_languages_have_the_same_keys_and_placeholders():
    strings, en, de = messages("strings.json"), messages("translations/en.json"), messages("translations/de.json")
    assert strings == en
    assert set(en) == set(de)
    for key in en:
        assert set(re.findall(r"{(\w+)}", en[key])) == set(re.findall(r"{(\w+)}", de[key])), key


def test_every_key_used_in_the_code_has_a_text_and_every_text_is_used():
    texts, calls = messages("strings.json"), used()
    assert set(calls) <= set(texts), set(calls) - set(texts)
    assert set(texts) - set(calls) == set(SPECIAL)  # no text without a raise (but the special one)


def test_calls_pass_the_placeholders_their_text_uses():
    texts = messages("strings.json")
    for key, passed_sets in used().items():
        wanted = set(re.findall(r"{(\w+)}", texts[key]))
        for passed in passed_sets:
            assert wanted <= passed, (key, wanted - passed)
    for key, passed in SPECIAL.items():
        assert set(re.findall(r"{(\w+)}", texts[key])) == passed


def test_no_url_in_a_message():  # hassfest: URLs go through placeholders
    for path in ("strings.json", "translations/en.json", "translations/de.json"):
        assert not [k for k, v in messages(path).items() if "http" in v], path


# -- repair issues ---------------------------------------------------------
ISSUE_SPECIAL = {  # created outside Store._issue: key -> placeholders the call passes
    "flash_failed": {"name", "error"},
    "pin_pairing_failed": {"name", "address", "flasher_url"},
    "pairing_package_outdated": {"proxy"},
}


def issue_keys_in_code() -> dict[str, set[str]]:
    """Every issue key created in the package, with the placeholders its call site passes."""
    found: dict[str, set[str]] = {}
    for path in PKG.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None)
            if name == "_issue" and node.args and isinstance(node.args[0], ast.Constant):
                extra = {kw.arg for kw in node.keywords if kw.arg and kw.arg != "fixable"}
                found.setdefault(node.args[0].value, set()).update({"name"} | extra)
            elif name == "async_create_issue":
                for kw in node.keywords:
                    if kw.arg == "translation_key" and isinstance(kw.value, ast.Constant):
                        found.setdefault(kw.value.value, set()).update(ISSUE_SPECIAL.get(kw.value.value, set()))
    return found


def test_every_repair_issue_has_a_title_and_text_in_all_languages():
    keys = set(const_issue_keys()) | set(ISSUE_SPECIAL) | set(issue_keys_in_code())
    assert keys >= {"firmware_unverified", "flash_failed", "pin_pairing_failed", "pairing_package_outdated"}
    for path in ("strings.json", "translations/en.json", "translations/de.json"):
        issues = json.loads((PKG / path).read_text())["issues"]
        for key in keys:
            assert key in issues, (path, key)
            assert issues[key].get("title"), (path, key)
            assert issues[key].get("description") or issues[key].get("fix_flow"), (path, key)


def const_issue_keys() -> tuple[str, ...]:
    from custom_components.pvvx_ota import ISSUE_KEYS

    return ISSUE_KEYS


def test_repair_issue_texts_use_only_placeholders_the_call_passes():
    calls = issue_keys_in_code()
    assert set(const_issue_keys()) <= set(calls)  # every cleared key is raised somewhere
    for path in ("strings.json", "translations/en.json", "translations/de.json"):
        issues = json.loads((PKG / path).read_text())["issues"]
        for key, passed in calls.items():
            text = issues[key].get("title", "") + issues[key].get("description", "")
            assert set(re.findall(r"{(\w+)}", text)) <= passed | {"name"}, (path, key)


def test_common_ota_errors_have_a_text_of_their_own():
    from custom_components.pvvx_ota.errors import ota_error
    from custom_components.pvvx_ota.ota import (
        NotPvvxError,
        OtaDeviceError,
        OtaLinkLost,
        OtaSlowLink,
        OtaTimeout,
    )

    expected = {
        OtaTimeout: "sensor_timeout",
        OtaLinkLost: "link_lost",
        OtaSlowLink: "slow_link",
        NotPvvxError: "not_pvvx",
    }
    for cls, key in expected.items():
        err = ota_error("ATC", cls("English detail"))
        assert err.translation_key == key and err.translation_placeholders == {"name": "ATC"}
    other = ota_error("ATC", OtaDeviceError("status 3"))
    assert other.translation_key == "sensor_error" and other.translation_placeholders["error"] == "status 3"
