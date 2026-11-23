# Design notes

Background for contributors: why PIN pairing and the flash speed work the way they do. Measured with a few sensors and ESP32
proxies, so treat the numbers as indications, not guarantees. Firmware references are to
[pvvx/ATC_MiThermometer](https://github.com/pvvx/ATC_MiThermometer).

## PIN pairing

pvvx firmware with a PIN asks for pairing and requires an encrypted (secure connections, MITM) link before it serves the config and OTA
characteristics. Home Assistant's Bluetooth API cannot answer a passkey request, so the integration cannot pair by itself.

What works:

1. An ESPHome proxy with a generic `ble_client` pairs once. Its address and the PIN come from an API action (`pair_pvvx(mac, pin)`,
   package `esphome/pvvx_pin_pairing.yaml`); `on_passkey_request` answers with the PIN; the bond lives in the ESP32's flash. The PIN is
   held in RAM only and zeroed afterwards.
2. Afterwards Home Assistant connects **through that proxy only**, calls `pair()` before its first command (about 0.2 s, this encrypts the
   link with the stored key) and then reads, writes and flashes as for any sensor. A firmware update of a PIN sensor was done this way.

Decisions and lessons:

- **The path cannot be chosen.** habluetooth's connect wrapper ignores the `BLEDevice` it is given and picks the best-scored path. The
  integration therefore checks which scanner served the connection (`client._connected_scanner`, a private attribute) and drops the link
  before sending anything if it is not the bonded proxy ("not reachable via the paired proxy").
- `pair()` on a proxy **without** a bond stalls on the passkey request and leaves that proxy's security manager busy until it reboots. So
  when a proxy offers `pair_pvvx`, the flow goes to the PIN step instead of probing with `pair()`.
- habluetooth names an ESPHome proxy `<node> (<MAC>)`; the action name is derived from the node name alone.
- `pair_pvvx` removes any old bond for the sensor first: a stale or "Just Works" bond makes the sensor refuse with "insufficient
  authentication", and pairing then reuses it without asking for the passkey.
- The pairer holds one connection slot, hence the `esp32_ble: max_connections` note. A sensor only takes one connection, so Home Assistant
  waits for the package's `pvvx pairer busy` entity to go off before it connects.
- The PIN is stored in the config entry (redacted in diagnostics, never logged) so a lost bond can be repaired automatically.
- **Known sensor-side problem:** some sensors accept the PIN pairing, the link is encrypted, and the sensor still answers with
  status 5 (insufficient authentication) to the first protected write. Three passkey pairings through two proxies behaved the same, so it
  is not the proxy and not a stale bond on the proxy. It happens on sensors with older firmware or an old pairing entry in the sensor's
  small bond table; trying more proxies adds bonds and makes it worse. Fix: update the firmware with TelinkMiFlasher, set the PIN to 0 and
  save, then set it again (this clears the sensor's pairing memory).
- A pvvx flash can reset the BTHome bindkey flag (sometimes the key too). The integration restores the flag when the key survived and raises
  a repair issue otherwise.

## OTA speed

The first implementation needed about 130 to 145 ms per 16-byte block (about 7 blocks/s, 15 minutes or more per flash). Findings:

- The sensor requests 20 ms interval and latency 49 about 1 s after connecting (`ble.c`); Bluedroid on the proxy accepts that request,
  which overrides the proxy's own 7.5/10 ms parameters. Latency 49 turns off during OTA only on the sensor side.
- **Asking the proxy for fast parameters after that request** (`set_connection_params(6, 6, 0, 600)`: 7.5 ms, latency 0) made a flash
  about 8 times faster. It is requested right before the OTA start, again about 2 s after the connect and once after the start command.
  Failures or missing support (proxies before ESPHome 2026.10) are logged and ignored.
- Data blocks go out as write-without-response with a status read every 8 blocks (`ota.DATA_RESPONSE`, `ota.STATUS_EVERY`). Error 143 on a
  write is congestion on the proxy side and is absorbed by the status reads and the device's index/CRC checks.
- With 7.5 ms the steady state is about 12.7 ms per block (60 to 80 blocks/s), i.e. a flash of about 5,350 blocks takes roughly 70 to
  95 s. TelinkMiFlasher measured 25 to 45 ms per block in a browser.
- Short periodic dips (a few hundred ms) remain. Candidates: the sensor's flash page erase every 4 KiB (every 256 blocks) and the proxy's
  scan duty cycle / Wi-Fi coexistence (`connection_scan_window` lower on the proxy may help). Not settled.
- **Weak links are uniformly slow, not dippy**: a sensor at -78 dBm through a far proxy ran at 2 blocks/s (about 460 ms/block) and would
  have needed over 40 minutes. Hence the **throughput probe**: after the first windows, fewer than about 15 blocks/s means the fast
  parameters are requested once more; if it is still that slow the flash is stopped before the end marker, so the sensor keeps its old
  firmware and the battery is not wasted. A PIN sensor cannot switch to a closer proxy (its bond is on one proxy); re-pair it through a
  closer proxy instead.

## Development
The design background for PIN pairing and flash speed is in [docs/design-notes.md](docs/design-notes.md). Contributions are welcome.

```
uv venv --python 3.14 .venv && uv pip install --python .venv/bin/python -r requirements_test.txt
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy && .venv/bin/pytest
```
Protocol code lives in `custom_components/pvvx_ota/ota.py` (no HA imports), ported from pvvx's `utils/ble_ota.py`.

## Research: sensors that pair with a passkey but still refuse the first protected write (status 5)
Read-only desk research (no pairing attempts), 2026-11-23. Symptom: the pairing connection logs `passkey requested`, then
`auth success ... mode = 13` (bond + MITM + LE Secure Connections). HA's later connection re-encrypts with that bond (`mode = 13`),
and the first write to the command characteristic is refused with ATT error 5 (insufficient authentication). It happens for two
sensors ("sensor F" and "sensor H") through three proxies; other PIN sensors work with the same setup. Both were Just-Works-paired
before a PIN was involved. Confidence tags: **[high]** read in source, **[med]** inferred, **[low]** guess.

### What xiaomi_esp_flasher does (inteltryb93/xiaomi_esp_flasher, commit 658bc11)
- **It does not support PIN-protected sensors at all [high].** `grep` for passkey, io_capability, auth_req, `esp_ble_set_encryption`,
  `remove_bond` finds nothing in `components/`. Its `esp32_ble:` block only sets `max_connections: 1` (`yaml/xiaomi_esp_flasher.yaml:65-66`),
  so the ESP32 keeps the default IO capability (no input/output) and any pairing is Just Works.
- Its GATT client subclasses ESPHome's `BLEClientBase` (`telink_ble_client.h`), so it inherits the stock handling: answer the sensor's
  Security Request with `esp_ble_gap_security_rsp(..., true)` and nothing else. Reads and writes use `ESP_GATT_AUTH_REQ_NONE`
  (`telink_ble_client.cpp:119,138`), same as ESPHome's own `ble_client.ble_write`.
- A PIN sensor shows up as "no answer to the config command" and is only flagged `pin_required = true` (`xiaomi_esp_flasher.cpp:1272-1276`);
  the flow stops there. The 0x70 PIN command is only offered for sensors it can already write to (`step_set_pin_`, `xiaomi_esp_flasher.cpp:1593-1599`,
  `encode_pin_code` in `device_config.cpp:197`). The web GUI's PIN field is ported from pvvx's `pvvx_config.js:767` (`0x70` + LE32).
- Conclusion: nothing to copy for the pairing problem; our `keyboard_only` + `sc_mitm_bond` + `passkey_reply` package is already ahead of it. **[high]**

### What the pvvx firmware does (pvvx/ATC_MiThermometer master, Nov 2026)
- With a non-zero PIN the firmware sets (`src/ble.c:689-715`): security level `Authenticated_Paring_with_Encryption` (LE Security Mode 1
  Level 3), LE Secure Connections, MITM on, IO capability display-only; the passkey is the PIN (`app_host_event_callback`, `ble.c:417-421`);
  Security Request sent immediately for a new link and after 1000 ms for a reconnect (`ble.c:726`). **[high]**
- Command (0x1F1F) and OTA characteristics get `ATT_PERMISSIONS_SECURE_CONN_RDWR` = RDWR | SECURE_CONN | ENCRYPT | AUTHEN (`src/app_att.c:644-649`,
  `SDK/.../att.h:99-101`). **This is chosen once at boot** from the stored PIN: with PIN 0 they are plain RDWR. **[high]**
- The attribute check itself lives in the closed Telink stack library; only headers are public. Whether it uses the live pairing result or the level
  stored in the bond record (`smp_param_save_t`: `security_level`, `secure_conn`, `stk_method`, peer address, LTK; `smp.h:98-124`) is **not
  verifiable from source [low]**. The bond table holds **4 peers** (`SMP_BONDING_DEVICE_MAX_NUM`, `smp.h:74-76`); pvvx says "up to 4 last bindings" (issue 174).
- **PIN command 0x70** (`src/cmd_parser.c:537-552`): payload `70 <pin LE32>`, i.e. 5 bytes. If the value differs from the stored one it is written
  to flash; **only when it toggles between zero and non-zero** the firmware calls `bls_smp_eraseAllParingInformation()` and sets
  `CONNECTED_FLG_RESET_OF_DISCONNECT`, so the sensor **reboots when the link drops** (`ble.c:77-79`). Reply `70 01` = ok, `70 03` = flash write failed.
  Changing one PIN to another keeps the bond table. **[high]**
- History: PIN support and the secure-connection attribute permissions date from fw 1.2 to 3.1 (2021, README changelog; `git log -S`), nothing relevant changed
  after. Firmware version therefore does not explain F/H (agrees with the earlier finding). **[med-high]**
- Issues searched (pincode, PIN, bond, insufficient authentication, pairing, ESP32): nothing about ESP32 or ESPHome. Related: #174 (Windows/Android
  keep a bond made while the PIN was 0, so the PIN is never used until the host forgets the device; the PIN must be set with battery above ~40 % because
  the bonding/flash write needs current; "forgotten PIN: only a hardware flasher"), #247 and #376 (same stale-host-bond pattern; pvvx points to #174),
  #131 (clear PIN by writing 000000). No report of "passkey OK, still status 5". **[high that nothing is documented, med that it is unknown upstream]**

### Why F/H may behave differently (hypotheses, all unproven)
1. **A stale or lower-level record for the proxy's address in the sensor's 4-slot table** [low-med]. Fits "paired without PIN first", and with "three proxies"
   (each was Just-Works-paired before). Weakness: the sensor accepted encryption with the *new* key, so the stored LTK is the new one; only its
   security-level flag would be wrong.
2. **The sensor evaluates the level of the live pairing correctly, but not the one reloaded from flash on reconnect** [low]. This is the only hypothesis that
   predicts "a write on the pairing link itself succeeds, a write on a re-encrypted link does not".
3. Different bonded-peer identity (public vs random address, IRK exchange) between the pairing link and HA's link [low].

### Remote-fix candidates, in order
**A. Test + fix inside the pairing link (recommended first try).** In the `pair_pvvx` flow (stock `ble_client`), after pairing has finished (about 15-20 s
after connect, the existing delay) and before disconnecting, run `ble_client.ble_write` (ESPHome `ble_client/automation.h`, write with response) to service
`0x1F10`, characteristic `0x1F1F`, value `[0x70, 0x00, 0x00, 0x00, 0x00]` (PIN 0, no secret needed). Log the result (ESPHome logs status of a failed write).
- If it **succeeds** (sensor answers `70 01`, then on disconnect it reboots with an empty bond table and unprotected characteristics): the sensor is now a
  plain no-PIN sensor reachable through any proxy. HA connects normally, then sets the PIN again with the integration (0x70 + PIN) as the
  last step; that is the same zero to non-zero toggle (erases the bond table again, reboots on disconnect), after which `pair_pvvx` pairs on a clean table. This is exactly the
  TelinkMiFlasher "PIN 0, then PIN again" procedure, done by proxy. **[med that it works if the write is allowed on the pairing link]**
- If it is **refused with status 5 as well**: hypothesis 2 is out, the sensor refuses even a freshly authenticated link; nothing is lost except one more bond slot. The
  test result is useful either way, so run it with the failure logged and no retries.
- Does the write on the pairing link have a chance? The link was just authenticated by the passkey (mode 13) and the Telink stack knows the pairing result in RAM; only
  after reconnect it has to rely on flash. **[low-med]**
- **Risks:** (1) while the PIN is 0 the sensor is open to anyone in range until the PIN is set again: do step 2 right away. (2) The flash write plus
  sector erase needs battery (pvvx: above ~40 %; check the battery sensor first; a brown-out during the erase could corrupt the config sector). (3) Never write 0 on a sensor
  that already works: it would only force a re-pair. (4) No lock-out risk when the PIN is set back to the known value: the PIN is never changed to an unknown value. (5) The
  proxy's own bond for that sensor is stale after the reset: `pair_pvvx` already removes it before pairing; other proxies holding bonds for the sensor must forget
  them too (`ble_client.remove_bond`), or HA may pick one and fail to encrypt.
- Needs a new ESPHome action or option in the package and a reflash of the pairing proxy; ESPHome has no "on auth complete" trigger for `ble_client`, so use the fixed delay.

**B. Pair from an address the sensor has never seen.** A proxy that never talked to F/H (or a proxy whose Bluetooth address was changed) has no record in the sensor's
table. Pair it with `pair_pvvx`; if HA then writes successfully through it, hypothesis 1 is confirmed and that proxy can serve the sensor. Cheap, needs no sensor
write, but adds a slot to a 4-slot table, so keep the number of tries small. **[low-med]** Changing the ESP32's Bluetooth address from YAML is possible but not
a stock option; not recommended just for a test.

**C. Do not try:** repeated passkey pairings through more proxies (adds records without clearing them); changing PIN to a *different non-zero* value (does not erase the table);
`pair()` retries from HA (already shown to give the same status 5).

**D. Fallbacks that need someone on site:** TelinkMiFlasher, PIN 0 then PIN again from a device with a clean bond; or a hardware flasher ("erase everything" and reflash) if the PIN is lost.

### Open questions
- Whether F and H are exactly firmware-equal to the working sensors (read the version through any working channel before blaming the stack).
- Whether the sensor's `70 xx` reply after step A can be captured (needs a notification subscription on the same connection; not required to judge success, the
  disconnect-and-reboot plus an unprotected characteristic afterwards is proof enough).
