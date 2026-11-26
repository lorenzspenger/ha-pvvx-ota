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
