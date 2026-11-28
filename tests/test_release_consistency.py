"""The version is written by hand in three places; they must agree."""

import json
import re
from pathlib import Path

import custom_components.pvvx_ota  # noqa: F401  (the autouse fixtures patch it, also when this file runs alone)

ROOT = Path(__file__).parent.parent


def _pep440_to_semver(version: str) -> str:
    """`0.26.0b6` -> `0.26.0-beta.6`; a stable `0.26.0` stays."""
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:b(\d+))?", version)
    assert match, f"unexpected manifest version {version!r}"
    return f"{match[1]}-beta.{match[2]}" if match[2] else match[1]


def test_manifest_readme_and_changelog_agree():
    manifest = json.loads((ROOT / "custom_components/pvvx_ota/manifest.json").read_text())["version"]
    semver = _pep440_to_semver(manifest)

    readme = (ROOT / "README.md").read_text()
    match = re.search(r"\*\*Version (\S+?)\.\*\*", readme)
    assert match, "README has no '**Version X.**' line"
    assert match[1] == semver, f"README says {match[1]}, manifest says {manifest}"

    changelog = (ROOT / "CHANGELOG.md").read_text()
    sections = re.split(r"^## ", changelog, flags=re.MULTILINE)[1:]
    assert sections, "CHANGELOG has no sections"
    titles = [s.splitlines()[0].strip() for s in sections]
    # Between releases the top section is "Unreleased", followed by the released one.
    released = titles[1] if titles[0] == "Unreleased" else titles[0]
    body = sections[titles.index(released)]
    assert released == semver, f"top released CHANGELOG section is {released}, manifest says {manifest}"
    assert f"manifest version `{manifest}`" in body, f"section {released} lacks 'manifest version `{manifest}`'"
