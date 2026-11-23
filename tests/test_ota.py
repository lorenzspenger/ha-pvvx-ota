"""Protocol tests against a fake GATT client."""

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.pvvx_ota import ota
from custom_components.pvvx_ota.firmware import Catalog, check_local_image, parse_catalog

from .fake_gatt import FakeClient


def image(n=100, size_ok=True):
    """A minimal valid Telink image: KNLT at 8, total size at 0x18."""
    body = bytearray(b"\x00" * 8 + b"KNLT" + bytes(range(256)) * (n // 256 + 1))
    body[0x18:0x1C] = len(body).to_bytes(4, "little")
    return bytes(body)


def test_crc_and_packets():
    assert ota.crc16_modbus(b"123456789") == 0x4B37
    pk = list(ota.ota_packets(b"\x01" * 20))
    assert len(pk) == 3 and len(pk[0][1]) == 20
    assert pk[1][1][2:18] == b"\x01" * 4 + b"\xff" * 12
    assert pk[2][1] == bytes.fromhex("02ff0100feff")


def test_parse_cfg():
    assert ota.parse_cfg(bytes([0x55, 0x59, 0, 0, 0, 0, 10, 4, 0x8B, 0, 55, 0])) == (0x59, 0)
    assert ota.parse_cfg(bytes([0x55, 0x40] + [0] * 8 + [0x83, 0x0F])) == (0x40, 19)
    assert ota.parse_cfg(b"\x55\x59") == (None, None)
    assert ota.parse_cfg(b"\x00" * 12) == (None, None)


def test_versions():
    assert ota.fmt_version(0x59) == "5.9" and ota.fmt_version(None) is None
    assert ota.parse_version("5.9") == 0x59 and ota.parse_version("x") is None
    assert ota.fmt_version(89) == "5.9"  # firmware.json stores decimal 89


def test_check_image():
    ota.check_image(image())
    with pytest.raises(ota.FirmwareError, match="KNLT"):
        ota.check_image(b"\x00" * 64)
    big = bytearray(b"\x00" * 8 + b"KNLT" + b"\x00" * ota.MAX_BLE_OTA)
    big[0x18:0x1C] = len(big).to_bytes(4, "little")
    with pytest.raises(ota.FirmwareError, match="too big"):
        ota.check_image(bytes(big))
    with pytest.raises(ota.FirmwareError, match="truncated"):
        ota.check_image(image(2000)[:1500])  # e.g. a download cut short


async def test_read_cfg():
    c = FakeClient()
    assert await ota.read_cfg(c) == (0x59, 0)


async def test_read_cfg_no_reply():
    with pytest.raises(ota.NotPvvxError):
        await ota.read_cfg(FakeClient(no_reply=True), timeout=0.05)


async def test_flash_ok():
    c, pct = FakeClient(), []
    fw = image(200)
    await ota.flash(c, fw, pct.append)
    n = -(-len(fw) // 16)
    assert c.writes[:2] == [bytes.fromhex("00ff"), bytes.fromhex("01ff")]
    assert len(c.writes) == 2 + n + 1
    assert c.writes[-1][:2] == b"\x02\xff"
    assert c.status_reads == n // 8 + 1  # +1: flush before the end marker
    assert pct[-1] == 100 and pct == sorted(pct)


async def test_flash_end_marker_disconnect_is_success():
    await ota.flash(FakeClient(end_marker_raises=True), image())


async def test_flash_device_error():
    c = FakeClient(fail_status_at=5)
    with pytest.raises(ota.OtaDeviceError, match="CRC error in data"):
        await ota.flash(c, image(400))


async def test_flash_write_timeout():
    c = FakeClient(hang_write_at=4)
    with pytest.raises(ota.OtaTimeout, match="write"):
        await ota.flash(c, image(), write_timeout=0.05)


async def test_flash_total_timeout():
    c = FakeClient()
    with pytest.raises(ota.OtaTimeout):
        await ota.flash(c, image(2000), total_timeout=0.1)  # 0.3s sleep alone exceeds it


async def test_flash_rejects_bad_image_before_any_write():
    c = FakeClient()
    with pytest.raises(ota.FirmwareError):
        await ota.flash(c, b"junk" * 10)
    assert c.writes == []


CATALOG = {
    "version": 89,
    "betaver": 96,
    "custom": ["bin/ATC_v59.bin", "bin/MHO_C401_v59.bin", "?", "bin/MHO_C401N_v59.bin"],
}


def test_catalog():
    cat: Catalog = parse_catalog(CATALOG)
    assert cat.version == 0x59 and cat.beta == 96
    assert cat.image_for(1) == "bin/MHO_C401_v59.bin"
    assert cat.board_for(3) == "MHO_C401N"
    for bad in (None, 2, 99, -1):
        with pytest.raises(ota.FirmwareError):
            cat.image_for(bad)
    with pytest.raises(ota.FirmwareError):
        parse_catalog({"nope": 1})


def test_local_file_board_check():
    fw = image()
    check_local_image(fw, "MHO_C401_v59.bin", "MHO_C401")
    with pytest.raises(ota.FirmwareError, match="board"):
        check_local_image(fw, "MHO_C401N_v59.bin", "MHO_C401")  # prefix collision
    with pytest.raises(ota.FirmwareError, match="board"):
        check_local_image(fw, "firmware.bin", "MHO_C401")
    with pytest.raises(ota.FirmwareError, match="KNLT"):
        check_local_image(b"x" * 64, "MHO_C401_v59.bin", "MHO_C401")


def test_path_traversal_refused():
    cat = parse_catalog({"version": 1, "custom": ["bin/../../evil.bin"]})
    with pytest.raises(ota.FirmwareError):
        cat.image_for(0)


async def test_cancel_propagates():
    c = FakeClient(hang_write_at=3)
    t = asyncio.create_task(ota.flash(c, image()))
    await asyncio.sleep(0.05)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t


async def test_read_cfg_pairing_and_missing_char():
    class Pair(FakeClient):
        async def start_notify(self, char, cb):
            raise PermissionError("Insufficient authentication")

    class Missing(FakeClient):
        async def start_notify(self, char, cb):
            raise KeyError("characteristic not found")

    with pytest.raises(ota.PairingRequiredError, match="PIN"):
        await ota.read_cfg(Pair())
    with pytest.raises(ota.NotPvvxError):
        await ota.read_cfg(Missing())


async def test_data_blocks_use_write_without_response():
    c = FakeClient()
    await ota.flash(c, image(200))
    assert c.responses[:2] == [True, True]  # start commands are acknowledged
    assert not any(c.responses[2:])  # data blocks and end marker are not


async def test_final_status_read_precedes_end_marker():
    order = []

    class Spy(FakeClient):
        async def read_gatt_char(self, char):
            order.append("read")
            return await super().read_gatt_char(char)

        async def write_gatt_char(self, char, data, response):
            if data[:2] == b"\x02\xff":
                order.append("marker")
            await super().write_gatt_char(char, data, response)

    await ota.flash(Spy(), image(100))
    assert order[order.index("marker") - 1] == "read"


async def test_error_in_final_status_blocks_end_marker():
    c = FakeClient(fail_status_at=0)
    with pytest.raises(ota.OtaDeviceError):
        await ota.flash(c, image(40))  # 3 blocks, no periodic read; only the final one runs
    assert not any(w[:2] == b"\x02\xff" for w in c.writes)


async def test_device_rejects_image_after_end_marker():
    with pytest.raises(ota.OtaDeviceError, match="Firmware CRC check"):
        await ota.flash(FakeClient(after_marker="crc_error"), image(100))


async def test_device_that_does_not_reboot_is_an_error():
    with pytest.raises(ota.OtaDeviceError, match="did not reboot"):
        await ota.flash(FakeClient(after_marker="stay"), image(100))


async def test_download_reads_whole_body():
    from custom_components.pvvx_ota.firmware import FirmwareSource

    fw = image(86000)

    class Content:
        async def iter_chunked(self, n):
            for i in range(0, len(fw), 1000):  # arrives in many pieces
                yield fw[i : i + 1000]

    class Resp:
        content = Content()

        def raise_for_status(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Session:
        def get(self, *a, **k):
            return Resp()

    assert await FirmwareSource(Session()).async_download("bin/ATC_v59.bin") == fw


async def test_status_every_is_tunable():
    c = FakeClient()
    await ota.flash(c, image(1000), status_every=32)
    n = -(-len(image(1000)) // 16)
    assert c.status_reads == n // 32 + 1


async def test_refused_read_means_pin_and_no_pairing_is_attempted():
    """Without a PIN pvvx firmware has no security at all, so any 'insufficient encryption/authentication' is a PIN:
    final, and no Just Works pairing (which cannot satisfy the PIN's authenticated link anyway)."""
    for kind in (True, False):
        c = FakeClient(needs_pairing=True, pair_helps=kind)  # even a pair() that would 'help' is never called
        with pytest.raises(ota.PairingRequiredError, match="PIN"):
            await ota.read_cfg_raw(c)
        assert c.pair_calls == 0


async def test_no_pin_reads_directly():
    c = FakeClient()
    await ota.read_cfg(c)
    assert c.pair_calls == 0


async def test_refused_read_leaves_no_notification_subscribed():
    class Sticky(FakeClient):
        subscribed = False

        async def start_notify(self, char, cb):
            self.subscribed = True
            await super().start_notify(char, cb)

        async def stop_notify(self, char):
            self.subscribed = False
            await super().stop_notify(char)

    c = Sticky(needs_pairing=True)
    with pytest.raises(ota.PairingRequiredError):
        await ota.read_cfg_raw(c)
    assert c.subscribed is False


# -- connection parameters for the transfer ------------------------------------------------
class ParamsClient(FakeClient):
    """A client that has set_connection_params (habluetooth wrapper -> bleak-esphome)."""

    def __init__(self, *args, fail=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.params_calls: list[tuple] = []
        self.order: list[str] = []
        self.fail = fail

    async def set_connection_params(self, a, b, c, d):
        self.order.append("params")
        self.params_calls.append((a, b, c, d))
        if self.fail:
            raise self.fail

    async def write_gatt_char(self, char, data, response):
        if char == ota.OTA_CHAR:
            self.order.append(data.hex()[:4])
        return await super().write_gatt_char(char, data, response)


async def test_flash_requests_fast_connection_before_and_after_start(caplog):
    caplog.set_level("DEBUG")
    c = ParamsClient()
    await ota.flash(c, image(200))
    assert c.params_calls == [(6, 6, 0, 600)] * 3
    assert c.order[:5] == ["params", "00ff", "01ff", "params", "params"]  # before, right after 01ff, then delayed
    assert (
        "Connection params (before start, +" in caplog.text and "since connect): requested interval 6-6" in caplog.text
    )
    assert "Connection params (after start" in caplog.text and "Connection params (delayed" in caplog.text


async def test_flash_works_without_set_connection_params(caplog):
    caplog.set_level("DEBUG")
    c = FakeClient()  # no such method (old bleak / backend)
    await ota.flash(c, image(200))
    assert c.writes and "has no set_connection_params" in caplog.text


@pytest.mark.parametrize("err", [NotImplementedError("old"), RuntimeError("boom"), ota.OtaError("x")])
async def test_failure_to_set_params_is_not_fatal(err, caplog):
    caplog.set_level("DEBUG")
    c = ParamsClient(fail=err)
    await ota.flash(c, image(200))
    assert c.writes and "not set" in caplog.text and "continuing" in caplog.text


async def test_params_call_that_hangs_is_cut_off(monkeypatch):
    monkeypatch.setattr(ota, "FAST_LINK_TIMEOUT", 0.05)

    class Hang(ParamsClient):
        async def set_connection_params(self, *a):
            await asyncio.sleep(3600)

    c = Hang()
    await ota.flash(c, image(200))
    assert c.writes  # the flash went ahead


async def test_delayed_rerequest_waits_until_2_s_after_connect(monkeypatch, caplog):
    caplog.set_level("DEBUG")
    monkeypatch.setattr(ota, "DELAYED_REREQUEST_AFTER", 0.3)
    c = ParamsClient()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await ota.flash(c, image(100), connected_at=t0)
    delayed = [m for m in caplog.messages if "Connection params (delayed" in m][0]
    at = float(delayed.split("+")[1].split(" s since connect")[0])
    assert at >= 0.29  # measured from the connect, not from the OTA start
    assert "Starting OTA" in caplog.text and "OTA link" not in caplog.text  # no context given


async def test_context_and_timing_windows_are_logged(caplog):
    caplog.set_level("DEBUG")
    c = ParamsClient()
    await ota.flash(
        c, image(300), status_every=8, connected_at=asyncio.get_running_loop().time(), context="via proxy X, RSSI -33"
    )
    assert "OTA link: via proxy X, RSSI -33" in caplog.text
    windows = [m for m in caplog.messages if m.startswith("OTA window")]
    assert windows and "blocks in" in windows[0] and "ms/block" in windows[0] and "since connect" in windows[0]


async def test_only_three_requests_no_adaptive_ones(monkeypatch, caplog):
    """Before the start, after it and the delayed one at +2 s: nothing else, even when a window is slow."""
    caplog.set_level("DEBUG")

    class Slow(ParamsClient):
        reads = 0

        async def read_gatt_char(self, char):
            Slow.reads += 1
            if Slow.reads > 2:
                await asyncio.sleep(0.03)
            return await super().read_gatt_char(char)

    c = Slow()
    await ota.flash(c, image(2000), status_every=8)
    assert len(c.params_calls) == 3
    assert not [m for m in caplog.messages if "re-request" in m]
    assert not hasattr(ota, "MAX_REREQUESTS")


async def test_window_logs_first_at_info_then_debug(caplog):
    caplog.set_level("DEBUG")
    c = ParamsClient()
    await ota.flash(c, image(600), status_every=8)
    wins = [r for r in caplog.records if r.getMessage().startswith("OTA window")]
    assert len(wins) >= 2
    assert wins[0].levelname == "INFO" and all(r.levelname == "DEBUG" for r in wins[1:])


# -- error paths ---------------------------------------------------------------------------------------
def test_parse_cfg_of_a_short_block_has_a_version_but_no_board():
    assert ota.parse_cfg(bytes([0x55, 0x59] + [0] * 8)) == (0x59, None)


async def test_stop_notify_errors_and_status_timeouts_are_contained():
    class Broken(FakeClient):
        async def stop_notify(self, char):
            raise RuntimeError("link lost")

    await ota._stop_notify_quietly(Broken())  # never raises

    class Hangs(FakeClient):
        async def read_gatt_char(self, char):
            await asyncio.sleep(60)

    with pytest.raises(ota.OtaTimeout, match="status read timed out"):
        await ota._status(Hangs(), 0.01, "test")


async def test_releasing_the_link_tolerates_odd_clients(caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    await ota._release_link(SimpleNamespace())  # a client without disconnect()

    class Gone:
        async def disconnect(self):
            raise ConnectionError("already gone")

    await ota._release_link(Gone())
    assert "ended with ConnectionError" in caplog.text


async def test_progress_is_reported_in_steps_over_a_long_image(caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    c, pct = FakeClient(), []
    await ota.flash(c, image(3000), pct.append)
    assert pct[-1] == 100 and len(set(pct)) > 10
    import re

    assert re.search(r"OTA \d+%: [\d.]+ blocks/s", caplog.text)


# -- throughput probe: a link that is too slow stops the flash before the end marker -------------------------------


class Paced(FakeClient):
    """A fake client whose writes take ``delay`` s; counts the connection-parameter requests and can speed up after the
    ``recover_after``-th one."""

    def __init__(self, delay, recover_after=None, **kw):
        super().__init__(**kw)
        self.delay, self.recover_after, self.param_requests = delay, recover_after, 0

    async def set_connection_params(self, *args):
        self.param_requests += 1
        if self.recover_after is not None and self.param_requests >= self.recover_after:
            self.delay = 0

    async def write_gatt_char(self, char, data, response):
        if self.delay:
            await asyncio.sleep(self.delay)
        await super().write_gatt_char(char, data, response)


@pytest.fixture
def strict_probe(monkeypatch):
    """Judge the rate after 0.05 s; below 1000 blocks/s counts as slow, so a 10 ms/block fake link is 'slow'."""
    monkeypatch.setattr(ota, "SLOW_PROBE_SECONDS", 0.05)
    monkeypatch.setattr(ota, "SLOW_BLOCKS_PER_S", 1000.0)


async def test_slow_link_asks_again_then_stops_before_the_end_marker(strict_probe):
    c = Paced(0.01)
    with pytest.raises(ota.OtaSlowLink, match=r"link too slow \(\d+\.\d blocks/s, connected via proxy X, RSSI -80\)"):
        await ota.flash(c, image(2000), context="connected via proxy X, RSSI -80")
    assert not c.marker_sent  # nothing was activated: the sensor keeps its old firmware
    assert c.param_requests == 4  # before start, after start, delayed, and the one new request on the slow link
    assert isinstance(ota.OtaSlowLink("x"), ota.OtaError)  # so the failed-flash repair issue applies


async def test_slow_link_that_recovers_after_the_new_request_carries_on(strict_probe):
    c = Paced(0.01, recover_after=4)  # the 4th request is the slow-link one; afterwards writes are instant
    result = await ota.flash(c, image(2000))
    assert c.marker_sent and result["blocks"] > 0 and c.param_requests == 4


async def test_a_normal_link_is_not_probed_again_or_stopped(monkeypatch):
    monkeypatch.setattr(ota, "SLOW_PROBE_SECONDS", 0.0)  # judged at the first window; the fake link is instant
    c = Paced(0)
    result = await ota.flash(c, image(2000))
    assert c.marker_sent and c.param_requests == 3 and result["blocks"] > 0  # only the three initial requests
