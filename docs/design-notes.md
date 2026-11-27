# Design notes

Background for contributors: why PIN pairing and the flash speed work the way they do. Measured with a few sensors and ESP32 proxies, so treat the numbers as indications, not guarantees. Firmware references are to [pvvx/ATC_MiThermometer](https://github.com/pvvx/ATC_MiThermometer).

## PIN pairing

pvvx firmware with a PIN asks for pairing and requires an encrypted (secure connections, MITM) link before it serves the config and OTA characteristics. Home Assistant's Bluetooth API cannot answer a passkey request, so the integration cannot pair by itself.

What works:

1. An ESPHome proxy with a generic `ble_client` pairs once. Its address and the PIN come from an API action (`pair_pvvx(mac, pin)`, package `esphome/pvvx_pin_pairing.yaml`); `on_passkey_request` answers with the PIN; the bond lives in the ESP32's flash. The PIN is held in RAM only and zeroed afterwards.
2. Afterwards Home Assistant connects **through that proxy only**, calls `pair()` before its first command (about 0.2 s, this encrypts the link with the stored key) and then reads, writes and flashes as for any sensor. A firmware update of a PIN sensor was done this way.

Decisions and lessons:

- **The path cannot be chosen.** habluetooth's connect wrapper ignores the `BLEDevice` it is given and picks the best-scored path. The integration therefore checks which scanner served the connection (`client._connected_scanner`, a private attribute) and drops the link before sending anything if it is not the bonded proxy ("not reachable via the paired proxy").
- `pair()` on a proxy **without** a bond stalls on the passkey request and leaves that proxy's security manager busy until it reboots. So when a proxy offers `pair_pvvx`, the flow goes to the PIN step instead of probing with `pair()`.
- habluetooth names an ESPHome proxy `<node> (<MAC>)`; the action name is derived from the node name alone.
- `pair_pvvx` removes any old bond for the sensor first: a stale or "Just Works" bond makes the sensor refuse with "insufficient authentication", and pairing then reuses it without asking for the passkey.
- The pairer holds one connection slot, hence the `esp32_ble: max_connections` note. A sensor only takes one connection, so Home Assistant waits for the package's `pvvx pairer busy` entity to go off before it connects.
- The PIN is stored in the config entry (redacted in diagnostics, never logged) so a lost bond can be repaired automatically.
- **Known sensor-side problem:** some sensors accept the PIN pairing, the link is encrypted, and the sensor still answers with status 5 (insufficient authentication) to the first protected write. Three passkey pairings through two proxies behaved the same, so it is not the proxy and not a stale bond on the proxy. It happens on sensors with older firmware or an old pairing entry in the sensor's small bond table; trying more proxies adds bonds and makes it worse. Fix: update the firmware with TelinkMiFlasher, set the PIN to 0 and save, then set it again (this clears the sensor's pairing memory).
- A pvvx flash clears the BTHome encryption flag; the key stays on the sensor, except across the 5.7/5.8 boundary (EEPROM rewrite) where it is replaced. The integration restores the flag when the key survived. Across the boundary it reads the key (command 0x18) in the flash connection, only if encryption is on, keeps it in memory only (never in state, attributes, entry data, diagnostics, logs or issues; dropped after the post-flash check, a failure or a cancel) and writes it back after the version check. The post-flash check itself is persisted as a `verify_pending` marker in the entry data (expected version, flag, timestamp; no secrets), set when the transfer starts and cleared when the check finishes; setup resumes it (marker older than 2 h: dropped). If the key cannot be written back (restart, failed write) encryption stays off and the `encryption_key_replaced` repair issue explains how to fix it.

## OTA speed

The first implementation needed about 130 to 145 ms per 16-byte block (about 7 blocks/s, 15 minutes or more per flash). Findings:

- The sensor requests 20 ms interval and latency 49 about 1 s after connecting (`ble.c`); Bluedroid on the proxy accepts that request, which overrides the proxy's own 7.5/10 ms parameters. Latency 49 turns off during OTA only on the sensor side.
- **Asking the proxy for fast parameters after that request** (`set_connection_params(6, 6, 0, 600)`: 7.5 ms, latency 0) made a flash about 8 times faster. It is requested right before the OTA start, again about 2 s after the connect and once after the start command. Failures or missing support (proxies before ESPHome 2026.10) are logged and ignored.
- Data blocks go out as write-without-response with a status read every 8 blocks (`ota.DATA_RESPONSE`, `ota.STATUS_EVERY`). Error 143 on a write is congestion on the proxy side and is absorbed by the status reads and the device's index/CRC checks.
- With 7.5 ms the steady state is about 12.7 ms per block (60 to 80 blocks/s), i.e. a flash of about 5,350 blocks takes roughly 70 to 95 s. TelinkMiFlasher measured 25 to 45 ms per block in a browser.
- Short periodic dips (a few hundred ms) remain. Candidates: the sensor's flash page erase every 4 KiB (every 256 blocks) and the proxy's scan duty cycle / Wi-Fi coexistence (`connection_scan_window` lower on the proxy may help). Not settled.
- **Weak links are uniformly slow, not dippy**: a sensor at -78 dBm through a far proxy ran at 2 blocks/s (about 460 ms/block) and would have needed over 40 minutes. Hence the **throughput probe**: after the first windows, fewer than about 15 blocks/s means the fast parameters are requested once more; if it is still that slow the flash is stopped before the end marker, so the sensor keeps its old firmware and the battery is not wasted. A PIN sensor cannot switch to a closer proxy (its bond is on one proxy); re-pair it through a closer proxy instead.

## Development
The design background for PIN pairing and flash speed is in [docs/design-notes.md](docs/design-notes.md). Contributions are welcome.

```
uv venv --python 3.14 .venv && uv pip install --python .venv/bin/python -r requirements_test.txt
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy && .venv/bin/pytest
```
Protocol code lives in `custom_components/pvvx_ota/ota.py` (no HA imports), ported from pvvx's `utils/ble_ota.py`.

## Private Bluetooth internals
Home Assistant has no supported way to say "connect through this proxy" or to ask which proxy a connection runs through, and a PIN sensor only answers through the proxy that holds its bond. All private access is in one module, [`bt_internals.py`](../custom_components/pvvx_ota/bt_internals.py): a client subclass that replaces habluetooth's `_async_get_best_available_backend_and_device` with "the paths of the paired scanner only", and a read of the connection's `_connected_scanner`. If a Home Assistant release removes either, the integration logs one warning and connects the normal way; a connection to a PIN-bonded sensor whose proxy cannot be verified is refused before anything is sent. `tests/test_pin_bond.py` simulates both attributes missing.

Some PIN sensors may still refuse the first protected write after a correct pairing; see the [known issue](pin-sensors.md#known-issue-the-sensor-refuses-after-a-correct-pairing).

## Background moved from the usage page

- **Discovery and the config flow.** Discovery matches `ATC_*` names, service data 0x181A (pvvx custom format) and 0xFCD2 (BTHome v2, shared with Shelly BLU and others). An advert is offered only if its name is `<MODEL>_<6 hex>`, it is the 0x181A format, or the MAC starts with `A4:C1:38`: passive proxies never see the name, and encrypted BTHome hides the payload. The manual picker lists every BTHome or 0x181A device. Only the confirm step connects, and it sends pvvx's configuration command 0x55 (no answer or no pvvx characteristic means "not a pvvx device").
- **The installed version needs a GATT connection.** pvvx's BTHome beacon carries no firmware-version object (checked in `bthome_beacon.[ch]`) and its 0x180A strings are stock-compatible, so the version is read from the configuration block and kept in the config entry. `firmware.json` stores the version byte in decimal (89 = 0x59 = V5.9, file `_v59`).
- **Devices.** Since Home Assistant 2026.10 a device belongs to exactly one config entry, so the update entity is attached to the existing BTHome device (found by its Bluetooth address) instead of creating a second one. If there is no BTHome device the integration creates its own and re-links the entities when a BTHome device appears or falls back when it is removed. The entity `unique_id` never changes, so entity ids and history survive.
- **Layouts.** pvvx's `python-interface` is out of date for 4.7 and later (it still has the temperature/humidity offsets where the firmware now has `flg3` and `event_adv_cnt`), so the layout comes from the firmware sources (`src/app.h`), see [version-layouts.md](version-layouts.md). The offsets live in the sensor-coefficient command `0x25` (firmware 4.8 and later) and are relative to the chip's factory value, which the integration takes from its own table.
- **Where a flash can stop.** The integration checks signature, size, header length and board before it connects, but does not compute the image CRC itself: the sensor checks it after the last block and refuses a bad image (it keeps the old firmware). Only a real disconnect after the end marker counts as the sensor rebooting. A link that stays up and silent for the supervision timeout we ask for plus a margin ends the flash as unconfirmed, and the version check afterwards decides.
- **Dangerous settings.** Setting the advertising type away from BTHome, the PHY switches, encryption and the PIN all follow the normal write rules (verified firmware only, read back, one operation at a time, never during a flash) and are opt-in.
