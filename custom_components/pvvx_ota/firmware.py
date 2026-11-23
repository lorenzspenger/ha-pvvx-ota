"""Firmware catalog (pvvx firmware.json) and downloads."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, replace

import aiohttp

from .const import CATALOG_MIN_INTERVAL, CATALOG_RETRY, CATALOG_TTL, MAX_DOWNLOAD, RAW_URL
from .ota import FirmwareError, board_of_filename, check_image

_PATH_RE = re.compile(r"bin/[A-Za-z0-9_-]+\.bin")
# betafw entries may be full URLs (as in pvvx's web flasher); only pvvx's own GitHub locations are accepted
_URL_RE = re.compile(r"https://(?:raw\.githubusercontent\.com|github\.com)/pvvx/[A-Za-z0-9_./%-]+\.bin")
_SKIP = {"?", "", "/"}
_HISTORY_ROW = re.compile(r"^\|\s*(\d{1,2})\.(\d{1,2})\s*\|\s*(.*?)\s*\|?\s*$")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
HISTORY_URL = RAW_URL + "README.md"
HISTORY_PAGE = "https://github.com/pvvx/ATC_MiThermometer#firmware-version-history"


@dataclass(frozen=True)
class Catalog:
    """Parsed firmware.json: latest stable version byte and image path per hardware id."""

    version: int
    beta: int | None
    images: tuple[str, ...]
    beta_images: tuple[str, ...] = ()  # "betafw" in firmware.json; usually absent

    def image_for(self, hw_id: int | None) -> str:
        """Image path for a hardware id; refuses unknown ids and '?' entries."""
        if hw_id is None or not 0 <= hw_id < len(self.images):
            raise FirmwareError(f"unknown hardware id {hw_id}; cannot pick a safe image")
        path = self.images[hw_id]
        if not _PATH_RE.fullmatch(path):
            raise FirmwareError(f"no BLE image for hardware id {hw_id}, use the web flasher")
        return path

    def beta_source_for(self, hw_id: int | None) -> tuple[str | None, str | None]:
        """(image path or URL, None) for the beta of a hardware id, or (None, why not)."""
        if self.beta is None:
            return None, "no beta firmware is published"
        if hw_id is None or not 0 <= hw_id < len(self.beta_images):
            return None, "no beta firmware for this hardware"
        src = self.beta_images[hw_id]
        if src in _SKIP:
            return None, "no beta firmware for this hardware"
        if ".." in src or not (_PATH_RE.fullmatch(src) or _URL_RE.fullmatch(src)):
            return None, "the beta image location is not an allowed pvvx GitHub address"
        return src, None

    def board_for(self, hw_id: int | None) -> str:
        """Board name (e.g. 'MHO_C401N') expected for a hardware id."""
        board = board_of_filename(self.image_for(hw_id))
        if board is None:
            raise FirmwareError(f"cannot derive board for hardware id {hw_id}")
        return board


def parse_catalog(data: dict) -> Catalog:
    """Parse the decoded firmware.json."""
    try:
        return Catalog(
            version=int(data["version"]),
            beta=int(data["betaver"]) if "betaver" in data else None,
            images=tuple(str(x) for x in data["custom"]),
            beta_images=tuple(str(x) for x in data["betafw"]) if isinstance(data.get("betafw"), list) else (),
        )
    except (KeyError, TypeError, ValueError) as err:
        raise FirmwareError(f"unexpected firmware.json format: {err}") from err


@dataclass(frozen=True)
class Target:
    """What the update entity offers: version byte, whether it is a beta, image source (None = stable image
    of the catalog), and an explanation when a requested beta is not used."""

    version: int
    is_beta: bool = False
    source: str | None = None
    note: str | None = None


def choose_target(catalog: Catalog, hw_id: int | None, beta_wanted: bool) -> Target:
    """Stable unless the beta option is on AND a beta image exists for this hardware, passes the same board check
    as stable images and is newer than the stable release (never a downgrade by itself)."""
    stable = Target(catalog.version)
    if not beta_wanted:
        return stable
    src, why = catalog.beta_source_for(hw_id)
    if src is None:
        return replace(stable, note=why)
    try:
        board = catalog.board_for(hw_id)
    except FirmwareError:
        return replace(
            stable, note="the board of the beta image cannot be verified (no stable image for this hardware)"
        )
    if board_of_filename(src) != board:
        return replace(stable, note="the beta image does not match this sensor's board")
    assert catalog.beta is not None
    if catalog.beta <= catalog.version:
        return replace(stable, note="no beta newer than the stable release")
    return Target(catalog.beta, True, src)


def parse_history(markdown: str) -> dict[int, str]:
    """Rows of pvvx's README "Firmware version history" table: version byte (5.9 -> 0x59) -> text (markdown kept)."""
    rows: dict[int, str] = {}
    in_section = False
    for line in markdown.splitlines():
        if line.startswith("#"):
            in_section = "firmware version history" in line.lower()
            continue
        if not in_section:
            continue
        m = _HISTORY_ROW.match(line)
        if m and (int(m[1]) <= 15 and int(m[2]) <= 15) and m[3].strip(" -"):
            rows[(int(m[1]) << 4) | int(m[2])] = m[3]
    return rows


def plain(text: str, limit: int = 255) -> str:
    """Markdown links/emphasis removed and shortened (release_summary is limited to 255 characters)."""
    t = _LINK.sub(r"\1", text).replace("`", "").replace("*", "").strip()
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def notes_between(history: dict[int, str], installed: int | None, target: int) -> str | None:
    """Markdown release notes of every version newer than ``installed`` up to ``target``, newest first."""
    versions = sorted(
        (
            v
            for v in history
            if (installed is None and v == target) or (installed is not None and installed < v <= target)
        ),
        reverse=True,
    )
    if not versions:
        return None
    return "\n\n".join(f"**V{v >> 4}.{v & 15}**: {history[v]}" for v in versions)


def check_local_image(fw: bytes, filename: str, expected_board: str) -> None:
    """Validate a user-supplied image against the board the device reported."""
    check_image(fw)
    board = board_of_filename(filename)
    if board is None or board != expected_board:
        raise FirmwareError(
            f"file name {filename!r} does not match this device's board {expected_board!r} "
            f"(expected {expected_board}_v<NN>.bin)"
        )


class FirmwareSource:
    """Cached, rate-limited access to the pvvx firmware repository."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._catalog: Catalog | None = None
        self._fetched = 0.0
        self._attempted = float("-inf")  # never attempted: monotonic() can be small right after a boot
        self._failures = 0  # consecutive failed fetches (reset by a success)
        self._lock = asyncio.Lock()  # concurrent callers share one fetch instead of racing the attempt marker
        self._history: dict[int, str] = {}
        self._history_fetched = 0.0
        self._history_attempted = float("-inf")
        self._history_failures = 0
        self._history_lock = asyncio.Lock()

    def summary(self) -> dict:
        """What is known about the catalog right now (diagnostics); no network access."""
        c = self._catalog
        return {
            "loaded": c is not None,
            "stable_version": None if c is None else f"{c.version >> 4}.{c.version & 15}",
            "beta_version": None if c is None or c.beta is None else f"{c.beta >> 4}.{c.beta & 15}",
            "images": 0 if c is None else sum(1 for i in c.images if i not in _SKIP),
            "beta_images": 0 if c is None else sum(1 for i in c.beta_images if i not in _SKIP),
            "age_s": None if not self._fetched else round(time.monotonic() - self._fetched),
        }

    @property
    def history(self) -> dict[int, str]:
        """The cached version history right now (empty until a fetch succeeded); no network access. Entities read this, so
        which sensors show release notes does not depend on who asked first."""
        return self._history

    async def async_history(self) -> dict[int, str]:
        """The firmware version history from pvvx's README, cached like the catalog; never raises (notes are optional).
        Concurrent callers share one fetch; with no history at all a failed fetch is retried after 1, 5 and then 15 minutes."""
        async with self._history_lock:
            now = time.monotonic()
            stale = not self._history or now - self._history_fetched > CATALOG_TTL
            if not self._history:
                wait = (
                    CATALOG_RETRY[min(self._history_failures, len(CATALOG_RETRY)) - 1]
                    if self._history_failures
                    else 0.0
                )
            else:
                wait = CATALOG_MIN_INTERVAL
            if stale and now - self._history_attempted >= wait:
                self._history_attempted = now
                try:
                    async with self._session.get(HISTORY_URL, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                        resp.raise_for_status()
                        rows = parse_history(await resp.text())
                    if rows:
                        self._history, self._history_fetched = rows, now
                        self._history_failures = 0
                    else:
                        self._history_failures += 1
                except Exception:  # noqa: BLE001 - release notes are a nicety: keep the last ones, never break an update check
                    self._history_failures += 1
            return self._history

    def _retry_wait(self) -> float:
        """Seconds to wait before fetching again: a known catalog is only refreshed at the min interval; with none, retry
        soon (short backoff after failures, none before the first attempt)."""
        if self._catalog is not None:
            return CATALOG_MIN_INTERVAL
        if not self._failures:
            return 0.0
        return CATALOG_RETRY[min(self._failures, len(CATALOG_RETRY)) - 1]

    async def async_catalog(self) -> Catalog:
        """Return the catalog, refetching at most every CATALOG_TTL. Concurrent callers wait for the one fetch in flight; with
        no catalog at all a failed fetch is retried after 1, 5 and then 15 minutes (the catalog is plain HTTP)."""
        async with self._lock:
            now = time.monotonic()
            stale = self._catalog is None or now - self._fetched > CATALOG_TTL
            if stale and now - self._attempted >= self._retry_wait():
                self._attempted = now
                try:
                    async with self._session.get(
                        RAW_URL + "firmware.json", timeout=aiohttp.ClientTimeout(total=20)
                    ) as resp:
                        resp.raise_for_status()
                        # raw.githubusercontent serves text/plain
                        self._catalog = parse_catalog(await resp.json(content_type=None))
                    self._fetched = now
                    self._failures = 0
                except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                    self._failures += 1
                    if self._catalog is None:
                        raise FirmwareError(f"cannot fetch firmware list: {err}") from err
            if self._catalog is None:
                raise FirmwareError("firmware list not available yet")
            return self._catalog

    async def async_download(self, path: str) -> bytes:
        """Download and validate one image."""
        if ".." in path or not (_PATH_RE.fullmatch(path) or _URL_RE.fullmatch(path)):
            raise FirmwareError(f"refusing odd image path {path!r}")
        url = path if path.startswith("https://") else RAW_URL + path
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                resp.raise_for_status()
                # StreamReader.read(n) returns only what is buffered, so read in a loop up to the limit
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.content.iter_chunked(8192):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD:
                        raise FirmwareError("image bigger than the BLE OTA limit")
                    chunks.append(chunk)
                fw = b"".join(chunks)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise FirmwareError(f"download failed: {err}") from err
        check_image(fw)
        return fw
