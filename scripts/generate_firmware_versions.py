"""Regenerate custom_components/pvvx_ota/firmware_versions.py from a local clone of pvvx/ATC_MiThermometer.

Usage: python scripts/generate_firmware_versions.py PATH_TO_CLONE [OUTPUT]

Read-only on the clone (only ``git show``, ``git ls-tree``, ``git log``, ``git rev-parse``); nothing is fetched or written
there. For every release in RELEASES it takes the image of every board from the release commit (file names are reused for
rebuilds, so a name alone does not identify a build), records the file path, SHA-256 and size, and the commit it was found at.
A board that has no image at the release commit gets the newest commit before the next release that added or changed the
file (the v4.4 tag, for instance, carries only some of that version's images); the releases after 5.7 (5.8, 5.9) use their
release commit only, because later commits rebuild the same file names. Evidence and background: docs/downgrade-research.md.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import subprocess
import sys
from pathlib import Path

# version byte -> release commit or tag (5.6 has no tag: the last commit with VERSION 0x56; 5.8 and 5.9 have none either)
RELEASES: dict[int, str] = {
    0x42: "v4.2",
    0x43: "v4.3",
    0x44: "v4.4",
    0x45: "v4.5",
    0x46: "v4.6",
    0x47: "v4.7",
    0x48: "v4.8",
    0x49: "v4.9",
    0x50: "v5.0",
    0x51: "v5.1",
    0x52: "v5.2",
    0x53: "v5.3",
    0x54: "v5.4",
    0x55: "v5.5",
    0x56: "e65881a",
    0x57: "v5.7",
    0x58: "9afe074",
    0x59: "9b65a24",
}
NO_FALLBACK = frozenset({0x56, 0x58, 0x59})  # later commits rebuild the same names: only the release commit counts
FLAT_UNTIL = 0x50  # up to 5.0 the images sit in the repository root, from 5.1 on in bin/
HEADER_BOARD_FROM = 0x47  # the board byte (0x14) is set from 4.6 on, but the 4.6 CGG1M image still carries the CGG1 one
HEAD = "HEAD"


def git(repo: Path, *args: str, binary: bool = False) -> str | bytes:
    out = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout
    return out if binary else out.decode()


def sha_of(repo: Path, ref: str) -> str:
    return str(git(repo, "rev-parse", f"{ref}^{{commit}}")).strip()


def boards_of_head(repo: Path) -> list[str]:
    """Board names with a BLE image in firmware.json at HEAD, in first-seen order."""
    data = json.loads(str(git(repo, "show", f"{HEAD}:firmware.json")))
    boards: list[str] = []
    for path in data["custom"]:
        m = re.fullmatch(r"bin/([A-Za-z0-9_-]+?)_v[0-9a-f]{2}\.bin", path)
        if m and m[1] not in boards:
            boards.append(m[1])
    return boards


def image_path(board: str, version: int) -> str:
    name = f"{board}_v{version:02x}.bin"
    return name if version <= FLAT_UNTIL else f"bin/{name}"


def blob(repo: Path, commit: str, path: str) -> bytes | None:
    try:
        return bytes(git(repo, "show", f"{commit}:{path}", binary=True))
    except subprocess.CalledProcessError:
        return None


def last_commit_before(repo: Path, before: str, path: str) -> str | None:
    out = str(git(repo, "log", "-1", "--format=%H", "--diff-filter=AM", before, "--", path)).strip()
    return out or None


def header_ok(fw: bytes) -> bool:
    return len(fw) >= 0x1C and fw[8:12] == b"KNLT" and struct.unpack_from("<I", fw, 0x18)[0] == len(fw)


def hw_first_versions(repo: Path, commits: dict[int, str]) -> dict[int, int]:
    """Hardware id (0..15, the enum in src/app.h) -> first release whose enum has it."""
    first: dict[int, int] = {}
    for version in sorted(commits):
        text = blob(repo, commits[version], "src/app.h")
        if text is None:
            continue
        body = text.decode(errors="replace")
        m = re.search(r"enum\s*\{([^{}]*)\}\s*HW_VERSION_ID", body, re.S)
        if not m:
            continue
        value = -1
        for line in m[1].splitlines():
            line = line.split("//")[0].strip()
            e = re.match(r"HW_VER_\w+\s*(?:=\s*(\d+))?", line)
            if not e:
                continue
            value = int(e[1]) if e[1] else value + 1
            if value < 15:  # 15 is HW_VER_UNKNOWN
                first.setdefault(value, version)
    return first


def main() -> None:
    repo = Path(sys.argv[1])
    out = (
        Path(sys.argv[2])
        if len(sys.argv) > 2
        else Path(__file__).parent.parent / "custom_components/pvvx_ota/firmware_versions.py"
    )
    commits = {v: sha_of(repo, ref) for v, ref in RELEASES.items()}
    versions = sorted(commits)
    boards = boards_of_head(repo)
    images: dict[int, dict[str, tuple[str, int, str | None]]] = {v: {} for v in versions}
    device_byte: dict[str, int] = {}
    problems: list[str] = []
    for i, version in enumerate(versions):
        nxt = commits[versions[i + 1]] if i + 1 < len(versions) else HEAD
        for board in boards:
            path = image_path(board, version)
            commit = commits[version]
            fw = blob(repo, commit, path)
            if fw is None and version not in NO_FALLBACK:
                later = last_commit_before(repo, nxt, path)
                if later is not None:
                    commit, fw = later, blob(repo, later, path)
            if fw is None:
                continue
            if not header_ok(fw) or len(fw) > 0x20000:
                problems.append(f"{board} {version:#x}: bad header or size")
                continue
            if version >= HEADER_BOARD_FROM:
                dev = fw[0x14]
                if device_byte.setdefault(board, dev) != dev:
                    problems.append(f"{board} {version:#x}: header board byte {dev:#x} != {device_byte[board]:#x}")
            images[version][board] = (
                hashlib.sha256(fw).hexdigest(),
                len(fw),
                None if commit == commits[version] else commit,
            )
    if problems:
        sys.exit("refusing to write:\n" + "\n".join(problems))
    hw_first = hw_first_versions(repo, commits)

    lines = [
        '"""Pinned pvvx firmware images per release (no Home Assistant imports). GENERATED by scripts/generate_firmware_versions.py',
        "from a clone of pvvx/ATC_MiThermometer: do not edit by hand, run the script again.",
        "",
        "File names are reused for rebuilds, so an image is identified by the commit it is fetched from and by its SHA-256, never by",
        "name alone. Background and evidence: docs/downgrade-research.md.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        'REPO = "pvvx/ATC_MiThermometer"',
        f"HEADER_BOARD_FROM = {HEADER_BOARD_FROM:#04x}  # from this release on byte 0x14 of the image is the board's device byte",
        f"FLAT_UNTIL = {FLAT_UNTIL:#04x}  # up to this release the images sit in the repository root, later ones in bin/",
        "",
        "# Release commit per version byte (5.6, 5.8 and 5.9 have no tag)",
        "RELEASE_COMMITS: dict[int, str] = {",
        *(f'    {v:#04x}: "{commits[v]}",' for v in versions),
        "}",
        "",
        "# Board -> device byte in the image header (0x14), the same in every release from 4.6 on",
        "DEVICE_BYTE: dict[str, int] = {",
        *(f'    "{b}": 0x{device_byte[b]:02X},' for b in boards if b in device_byte),
        "}",
        "",
        "# Hardware ids 0..14 (src/app.h enum): first release that knows the id; ids from 16 on belong to boards whose first image is",
        "# their first release (see IMAGES)",
        "HW_FIRST_VERSION: dict[int, int] = {",
        *(f"    {h}: {hw_first[h]:#04x}," for h in sorted(hw_first)),
        "}",
        "",
        "# version -> board -> (SHA-256, size, commit): the commit only where it is not the release commit",
        "IMAGES: dict[int, dict[str, tuple[str, int] | tuple[str, int, str]]] = {",
    ]
    for v in versions:
        lines.append(f"    {v:#04x}: {{")
        for board in boards:
            if board in images[v]:
                h, s, c = images[v][board]
                if c is None:
                    lines.append(f'        "{board}": ("{h}", {s}),')
                else:
                    lines += [
                        f'        "{board}": (',
                        f'            "{h}",',
                        f"            {s},",
                        f'            "{c}",',
                        "        ),",
                    ]
        lines.append("    },")
    lines.append("}")
    out.write_text("\n".join(lines) + "\n")
    count = sum(len(x) for x in images.values())
    print(f"wrote {out}: {count} images, {len(boards)} boards, {len(versions)} releases")


if __name__ == "__main__":
    main()
