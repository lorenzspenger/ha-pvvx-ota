"""Telink OTA protocol of pvvx ATC_MiThermometer custom firmware (no Home Assistant imports).

Ported from https://github.com/pvvx/ATC_MiThermometer/blob/master/utils/ble_ota.py
(which ports TelinkMiFlasher.html). The GATT client is any object with the bleak
``BleakClient`` methods used in :class:`GattClient`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import struct
from collections.abc import Callable, Iterator
from typing import Any, Protocol

_LOGGER = logging.getLogger(__name__)

OTA_CHAR = "00010203-0405-0607-0809-0a0b0c0d2b12"
CFG_CHAR = "00001f1f-0000-1000-8000-00805f9b34fb"
MAX_BLE_OTA = 0x20000  # bigger images need the web flasher's extended OTA
BLOCK = 16
STATUS_EVERY = 8  # blocks between status polls, as the web flasher does
SLOW_BLOCKS_PER_S = (
    15.0  # below this rate (normal: 60 and more) a flash is not worth the battery: stopped before the end marker
)
SLOW_PROBE_SECONDS = 3.0  # data seconds over which the rate is judged (at the next status window), once, then once more after a new request
OTA_ERRORS = [
    "Success",
    "Lost one or more packets",
    "CRC error in data",
    "Writing data to flash",
    "Lost last one or more packets",
    "Timeout",
    "Firmware CRC check",
]
WRITE_TIMEOUT = 10.0
CFG_TIMEOUT = 10.0
FLASH_TIMEOUT = 90 * 60.0
# Data blocks as write-without-response: with write-with-response every 16-byte block costs a full
# connection-event round trip (measured ~0.6 s/block through an ESPHome proxy). Loss is still caught by
# the status poll every STATUS_EVERY blocks.
DATA_RESPONSE = False
END_MARKER_GRACE = 8.0  # seconds to watch the device after the end marker


class OtaError(Exception):
    """Base class for OTA failures."""


class OtaTimeout(OtaError):
    """A GATT operation or the whole flash took too long."""


class OtaDeviceError(OtaError):
    """The device reported an OTA error status."""


class OtaSlowLink(OtaError):
    """The link is too slow for a flash (stopped before the end marker: the sensor keeps its old firmware)."""


class NotPvvxError(OtaError):
    """The device has no pvvx config characteristic or never answers command 0x55."""


class PairingRequiredError(OtaError):
    """The device asks for pairing, i.e. it has a PIN set; PIN-protected sensors are not supported by this integration."""


PAIRING_HINT = (
    "this sensor has a PIN set (it asks for pairing before it answers). PIN-protected sensors are not supported by "
    "this integration; remove the PIN in TelinkMiFlasher to use it"
)


def looks_like_pairing_error(err: BaseException) -> bool:
    """Heuristic on bleak/BlueZ/ESPHome error text for 'needs pairing/encryption'."""
    text = str(err).lower()
    return any(w in text for w in ("authentic", "encrypt", "pairing", "insufficient", "not permitted"))


class FirmwareError(OtaError):
    """The firmware image is unusable or does not fit the device."""


class GattClient(Protocol):
    """Subset of bleak's BleakClient used here."""

    async def start_notify(self, char: str, callback: Callable[..., Any], /) -> None: ...
    async def stop_notify(self, char: str, /) -> None: ...
    async def write_gatt_char(self, char: str, data: bytes, response: bool) -> None: ...
    async def read_gatt_char(self, char: str, /) -> bytearray: ...


def crc16_modbus(data: bytes) -> int:
    """CRC-16/MODBUS."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def ota_packets(fw: bytes) -> Iterator[tuple[int, bytes]]:
    """Yield (index, packet): 16-byte blocks as [idx LE16][data][crc16 LE16], then the end marker."""
    fw += b"\xff" * (-len(fw) % BLOCK)
    n = len(fw) // BLOCK
    for i in range(n):
        p = struct.pack("<H", i) + fw[i * BLOCK : (i + 1) * BLOCK]
        yield i, p + struct.pack("<H", crc16_modbus(p))
    yield n, bytes.fromhex("02ff") + struct.pack("<HH", n - 1, ~(n - 1) & 0xFFFF)


def parse_cfg(r: bytes) -> tuple[int | None, int | None]:
    """Parse the reply to command 0x55 -> (firmware version byte, hardware id)."""
    if len(r) < 10 or r[0] != 0x55:
        return None, None
    ver = r[1]
    if len(r) < 12:
        return ver, None
    hver = r[11] & 0x87 if ver < 0x36 else r[11]
    if ver >= 0x48:
        return ver, hver
    hw = hver & 0x0F
    return ver, 16 + (r[10] & 0x7F) if hw == 0x0F else hw


def fmt_version(v: int | None) -> str | None:
    """Version byte 0x59 -> '5.9'."""
    return None if v is None else f"{v >> 4}.{v & 15}"


def parse_version(s: str) -> int | None:
    """'5.9' -> 0x59."""
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", s)
    if not m or int(m[1]) > 15 or int(m[2]) > 15:
        return None
    return (int(m[1]) << 4) | int(m[2])


def check_image(fw: bytes) -> None:
    """Reject anything that is not a Telink image of BLE-OTA size."""
    if len(fw) < 16 or fw[8:12] != b"KNLT":
        raise FirmwareError("not a Telink firmware image (missing KNLT signature)")
    if len(fw) > MAX_BLE_OTA:
        raise FirmwareError("image too big for BLE OTA, use the web flasher")
    declared = struct.unpack_from("<I", fw, 0x18)[0]  # Telink header: image size
    if declared != len(fw):
        raise FirmwareError(f"image is truncated or corrupt: header says {declared} bytes, got {len(fw)}")


def board_of_filename(name: str) -> str | None:
    """'bin/MHO_C401_v59.bin' -> 'MHO_C401'; None if it does not look like a release image."""
    m = re.fullmatch(r"(?:.*/)?([A-Za-z0-9_-]+?)_v[0-9a-fA-F]+\.bin", name)
    return m[1] if m else None


async def read_cfg(client: GattClient, timeout: float = CFG_TIMEOUT) -> tuple[int | None, int | None]:
    """Query version and hardware id (see read_cfg_raw)."""
    return parse_cfg(await read_cfg_raw(client, timeout))


async def read_cfg_raw(client: GattClient, timeout: float = CFG_TIMEOUT) -> bytes:
    """Read the config block (reply to command 0x55).

    Without a PIN, pvvx firmware uses no security at all (No_Security, plain read/write permissions). With a PIN set it
    asks for pairing and gives the command and OTA characteristics ATT_PERMISSIONS_SECURE_CONN_RDWR. This integration
    does not support PIN-protected sensors (design decision, not a Bluetooth impossibility: other tools can reach them
    by pairing/reconnecting), so "insufficient encryption/authentication" on the first attempt is final:
    PairingRequiredError, no pairing attempts."""
    return await _transact(client, b"\x55", timeout)


async def send_command(
    client: GattClient, request: bytes, expect: int, timeout: float = CFG_TIMEOUT, log: bool = True
) -> bytes:
    """One command on the config characteristic; returns the notification starting with ``expect``.

    ``log=False`` keeps the traffic out of the log (bindkey, PIN)."""
    return await _transact(client, request, timeout, expect, log)


async def write_cfg(client: GattClient, payload: bytes, timeout: float = CFG_TIMEOUT) -> bytes:
    """Write the config (command 0x55 + cfg bytes, no version byte); returns the block the device reports back."""
    return await _transact(client, b"\x55" + payload, timeout)


async def _stop_notify_quietly(client: GattClient) -> None:
    try:
        async with asyncio.timeout(2):
            await client.stop_notify(CFG_CHAR)
    except Exception:  # noqa: BLE001 - best effort
        pass


async def _transact(client: GattClient, request: bytes, timeout: float, expect: int = 0x55, log: bool = True) -> bytes:
    """Send ``request`` to the config characteristic and return the 0x55 notification the device answers with."""
    reply: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

    def on_notify(_char: object, data: bytearray) -> None:
        if data[:1] == bytes([expect]) and not reply.done():
            reply.set_result(bytes(data))

    try:
        async with asyncio.timeout(timeout):
            await client.start_notify(CFG_CHAR, on_notify)
            await client.write_gatt_char(CFG_CHAR, request, response=True)
            result = await reply
    except TimeoutError as err:
        # a pvvx device answers within milliseconds once connected
        _LOGGER.debug("No %#04x notification within %s s", expect, timeout)
        await _stop_notify_quietly(client)
        raise NotPvvxError("no answer to the version request") from err
    except Exception as err:
        _LOGGER.debug("Config transaction failed: %s: %s", type(err).__name__, err if log else "(details withheld)")
        await _stop_notify_quietly(client)  # leave no stale subscription behind
        if looks_like_pairing_error(err):
            raise PairingRequiredError(f"{PAIRING_HINT} [{type(err).__name__}: {err}]") from err
        raise NotPvvxError(f"no pvvx config characteristic: {err}") from err
    if log:
        _LOGGER.debug("Config reply: %s", result.hex())
    await _stop_notify_quietly(client)
    return result


async def flash(
    client: GattClient,
    fw: bytes,
    progress: Callable[[int], None] | None = None,
    write_timeout: float = WRITE_TIMEOUT,
    total_timeout: float = FLASH_TIMEOUT,
    status_every: int = STATUS_EVERY,
    connected_at: float | None = None,
    context: str = "",
) -> dict[str, Any]:
    """Send an image. Returns after the end marker; the device then verifies and reboots.

    ``connected_at`` (event-loop time of the connect) and ``context`` (which proxy/RSSI) only feed the timing logs."""
    check_image(fw)
    try:
        async with asyncio.timeout(total_timeout):
            return await _flash(client, fw, progress, write_timeout, status_every, connected_at, context)
    except TimeoutError as err:
        raise OtaTimeout("flash timed out") from err


async def _write(client: GattClient, data: bytes, response: bool, timeout: float) -> None:
    try:
        async with asyncio.timeout(timeout):
            await client.write_gatt_char(OTA_CHAR, data, response=response)
    except TimeoutError as err:
        raise OtaTimeout("GATT write timed out") from err


async def _status(client: GattClient, wt: float, where: str) -> None:
    """Read the OTA status; this is also a sync point: queued no-response writes are processed first."""
    try:
        async with asyncio.timeout(wt):
            st = await client.read_gatt_char(OTA_CHAR)
    except TimeoutError as err:
        raise OtaTimeout("status read timed out") from err
    if st and st[0]:
        msg = OTA_ERRORS[st[0]] if st[0] < len(OTA_ERRORS) else str(st[0])
        raise OtaDeviceError(f"OTA error {where}: {msg}")


async def _release_link(client: GattClient) -> None:
    """Disconnect right after the end marker: the sensor reboots only once the link is gone, and a link left open
    keeps it from advertising (and a proxy slot busy) for minutes."""
    disconnect = getattr(client, "disconnect", None)
    if disconnect is None:
        return
    try:
        async with asyncio.timeout(DISCONNECT_AFTER_FLASH_TIMEOUT):
            await disconnect()
        _LOGGER.debug("Link released after the end marker")
    except Exception as err:  # noqa: BLE001 - e.g. already gone because the sensor rebooted
        _LOGGER.debug("Releasing the link after the end marker ended with %s: %s", type(err).__name__, err)


async def _after_marker(client: GattClient) -> None:
    """Watch what the device does after the end marker.

    Success: it checks the image CRC and reboots, so the link drops (reads fail).
    Failure: it stays connected and reports a status code (e.g. "Firmware CRC check").
    """
    for second in range(int(END_MARKER_GRACE) + 1):
        await asyncio.sleep(1 if END_MARKER_GRACE else 0)
        try:
            async with asyncio.timeout(3):
                st = await client.read_gatt_char(OTA_CHAR)
        except Exception as err:  # noqa: BLE001 - any failure here means the link is gone
            _LOGGER.debug("After the end marker the device went away (%s): it is rebooting", type(err).__name__)
            return
        code = st[0] if st else 0
        _LOGGER.debug("Status %s s after the end marker: %s", second, code)
        if code:
            msg = OTA_ERRORS[code] if code < len(OTA_ERRORS) else str(code)
            raise OtaDeviceError(f"device rejected the image after the end marker: {msg}")
    _LOGGER.warning(
        "Device is still connected %s s after the end marker with status 0: it did not reboot", END_MARKER_GRACE
    )
    raise OtaDeviceError("device did not reboot after the end marker; the new firmware was not activated")


# Connection parameters asked of the proxy for the transfer: min/max interval 6 (x1.25 ms = 7.5 ms), latency 0,
# supervision timeout 600 (x10 ms = 6 s). The sensor asks for 20 ms / latency 49 about 1 s after connecting and the proxy's
# Bluedroid accepts that, so the request is made right before the start command and once more after it.
FAST_LINK = (6, 6, 0, 600)
FAST_LINK_TIMEOUT = 10.0
DISCONNECT_AFTER_FLASH_TIMEOUT = 8.0
DELAYED_REREQUEST_AFTER = 2.0  # s since connect: the sensor asks for 20 ms/latency 49 about 1 s after connecting
TIMING_LOG_SECONDS = 15.0  # log every status window for this long after the start


async def request_fast_link(client: GattClient, when: str) -> bool:
    """Ask the proxy for a short connection interval (habluetooth -> bleak-esphome, feature flag
    CONNECTION_PARAMS_SETTING). Never fatal: every failure or missing method is logged and the flash goes on.
    Returns True if the call went through (older layers only log a warning and return, which looks the same here)."""
    method = getattr(client, "set_connection_params", None)
    if method is None:
        _LOGGER.debug(
            "Connection params (%s): this client has no set_connection_params; link stays as negotiated", when
        )
        return False
    try:
        async with asyncio.timeout(FAST_LINK_TIMEOUT):
            await method(*FAST_LINK)
    except Exception as err:  # noqa: BLE001 - old proxy firmware, no backend support, timeout...
        _LOGGER.debug("Connection params (%s): not set (%s: %s); continuing", when, type(err).__name__, err)
        return False
    _LOGGER.debug(
        "Connection params (%s): requested interval %s-%s (x1.25 ms), latency %s, timeout %s (x10 ms) via %s",
        when,
        FAST_LINK[0],
        FAST_LINK[1],
        FAST_LINK[2],
        FAST_LINK[3],
        type(client).__module__,
    )
    return True


async def _flash(
    client: GattClient,
    fw: bytes,
    progress: Callable[[int], None] | None,
    wt: float,
    status_every: int = STATUS_EVERY,
    connected_at: float | None = None,
    context: str = "",
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    t_conn = connected_at if connected_at is not None else loop.time()

    def since_connect() -> str:
        return f"+{loop.time() - t_conn:.2f} s since connect"

    if context:
        _LOGGER.debug("OTA link: %s", context)
    await request_fast_link(client, f"before start, {since_connect()}")
    await _write(client, bytes.fromhex("00ff"), True, wt)  # start
    await _write(client, bytes.fromhex("01ff"), True, wt)
    await request_fast_link(client, f"after start, {since_connect()}")
    # The sensor's own connection-parameter request (20 ms, latency 49) can land ~1 s after the connect, after our
    # first requests: wait until DELAYED_REREQUEST_AFTER since the connect, then ask once more.
    wait = DELAYED_REREQUEST_AFTER - (loop.time() - t_conn)
    if connected_at is not None and wait > 0:  # without a known connect time there is nothing to wait for
        await asyncio.sleep(wait)
    await request_fast_link(client, f"delayed, {since_connect()}")
    await asyncio.sleep(0.3)
    total = -(-len(fw) // BLOCK)
    last_pct = -1
    _LOGGER.debug(
        "Starting OTA: %s bytes, %s blocks, status every %s blocks (%s)", len(fw), total, status_every, since_connect()
    )
    t_start = t_mark = t_win = loop.time()
    status_time, status_n, blocks_mark, blocks_win = 0.0, 0, 0, 0
    first_window = True
    # throughput probe: judged at the first status window after SLOW_PROBE_SECONDS; a slow link gets one new request for the
    # fast connection parameters and another probe period, then the flash stops (nothing is activated before the end marker)
    probe_open, probe_asked, probe_t0, probe_b0 = True, False, t_start, 0
    for i, p in ota_packets(fw):
        if i == total:
            # Make sure every data block has really reached and been accepted by the device
            # before the end marker; the final status read flushes the queued writes.
            await _status(client, wt, "after the last block")
            # End marker: on success the device verifies the image and reboots, dropping the link.
            _LOGGER.debug(
                "All %s blocks sent and accepted after %.0f s, sending end marker", total, loop.time() - t_start
            )
            try:
                await _write(client, p, False, wt)
            except OtaTimeout:
                raise
            except Exception as err:  # noqa: BLE001 - disconnect while rebooting is expected
                _LOGGER.debug("End marker write ended with %s: %s (device rebooting?)", type(err).__name__, err)
            await _after_marker(client)
            await _release_link(client)
            break
        await _write(client, p, DATA_RESPONSE, wt)
        if (i + 1) % status_every == 0:
            t0 = loop.time()
            await _status(client, wt, f"at block {i}")
            status_time += loop.time() - t0
            status_n += 1
            now_w = loop.time()
            n_win = i + 1 - blocks_win
            win_s = max(now_w - t_win, 1e-9)
            rate = n_win / win_s
            if now_w - t_start <= TIMING_LOG_SECONDS:
                # the first window at INFO (the link right after the start), the rest at DEBUG
                _LOGGER.log(
                    logging.INFO if first_window else logging.DEBUG,
                    "OTA window +%.2f s since start (%s): %s blocks in %.3f s = %.1f ms/block (%.1f blocks/s)",
                    now_w - t_start,
                    since_connect(),
                    n_win,
                    win_s,
                    1000 * win_s / n_win,
                    rate,
                )
            first_window = False
            t_win, blocks_win = loop.time(), i + 1
            if probe_open and now_w - probe_t0 >= SLOW_PROBE_SECONDS:
                probe_rate = (i + 1 - probe_b0) / (now_w - probe_t0)
                if probe_rate >= SLOW_BLOCKS_PER_S:
                    probe_open = False
                elif not probe_asked:
                    probe_asked = True
                    _LOGGER.debug(
                        "OTA link slow (%.1f blocks/s, below %.0f): asking for the fast connection parameters again",
                        probe_rate,
                        SLOW_BLOCKS_PER_S,
                    )
                    await request_fast_link(client, f"slow link, {since_connect()}")
                    probe_t0, probe_b0 = loop.time(), i + 1
                else:
                    where = f", {context}" if context else ""
                    raise OtaSlowLink(
                        f"link too slow ({probe_rate:.1f} blocks/s{where}): the flash was stopped before the end marker to save "
                        "the battery, the sensor keeps its old firmware; try again later or move the sensor's pairing to a closer proxy"
                    )
            pct = (i + 1) * 100 // total
            if progress and pct != last_pct:
                last_pct = pct
                progress(pct)
                if pct // 10 != (pct - 1) // 10 or pct == 100:
                    now = loop.time()
                    done = i + 1 - blocks_mark
                    _LOGGER.debug(
                        "OTA %s%%: %.1f blocks/s over %.1f s; status reads %s x %.2f s (%.0f%% of the time)",
                        pct,
                        done / max(now - t_mark, 1e-9),
                        now - t_mark,
                        status_n,
                        status_time / max(status_n, 1),
                        100 * status_time / max(now - t_mark, 1e-9),
                    )
                    t_mark, blocks_mark, status_time, status_n = now, i + 1, 0.0, 0
    if progress:
        progress(100)
    return {"blocks": total, "bytes": len(fw), "seconds": round(loop.time() - t_start, 1), "status_every": status_every}
