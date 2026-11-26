# Changelog

## 0.23.0

Git tag `v0.23.0`, manifest version `0.23.0`. The first stable release, based on 0.23.0-beta.6: the betas brought the firmware update entity with queue and slow-link protection, the sensor settings, experimental PIN sensors through an ESPHome proxy, translated errors, strict typing and a long list of fixes from a full code review.

- **Shorter wait after a flash**: no other connection is opened for 30 s (was 150 s), the first version check runs after 30 s (was 90 s) and is retried every 30 s (was 45 s), still 8 attempts. A pvvx sensor reboots in seconds; the fix that matters (release the link right after the end marker, no connection during the reboot) is unchanged.
- **A normal reboot after the end marker is no longer reported as "unverified"** (found in a test reflash over a weak proxy): the sensor reboots at once but the link only counts as lost when the supervision timeout (6 s, longer through a proxy) runs out, so silent reads are now watched for 10 s and only a link that is still up after that is unverified; a disconnect inside the window is success and a status code is still a failure.
- **Reflash button text corrected**: a flash takes about 1.5 to 3 minutes on a good link and longer on a weak one, not 10 to 20 minutes.

## 0.23.0-beta.6

Git tag `v0.23.0-beta.6`, manifest version `0.23.0b6`.

- **Cleanup, no behaviour change**: the unused `name` parameter is gone from every settings entity, the entity helper `get_store` is gone, and the capabilities an entry stores are computed by one function (`config.capabilities_for`) instead of a fake config block.
- **The flash routine is split** into the start sequence, a slow-link probe, the timing log and the end-of-transfer step; the flash tests pass unchanged.
- **A newer flash replaces the waiting post-flash version check** instead of stacking timers, and unloading cancels it.
- **Tests renamed after what they check** (`test_connection_and_firmware_errors`, `test_store_behaviour`, `test_update_entity`); the device-registry test now uses real registry changes instead of a fake event.
- **CI**: all Python test and lint dependencies (including the Bluetooth libraries) are pinned, Dependabot watches the actions and the pins, and the workflow says why `hassfest` and the HACS action stay on their documented moving refs.

## 0.23.0-beta.5

Git tag `v0.23.0-beta.5`, manifest version `0.23.0b5`.

- **Translated error messages**: every error the integration raises is now a translation key with placeholders (English and German), so messages follow the user's language; a test checks that keys, texts and placeholders match.
- **Input errors are `ServiceValidationError`**: a bad key or PIN, a file path that is not allowed or cannot be read, a low battery without `force` and a missing `confirm` are reported as invalid input, not as failures.
- **Shared state in its own module** (`shared.py`): the import cycles and the function-local imports are gone; behaviour is unchanged.
- **One helper attaches the entities to the BTHome device** (`registry.attach_to_device`) instead of two copies of the same code.
- **Strict typing**: mypy strict passes on the whole package without per-module overrides or ignored imports; `quality_scale.yaml` now lists `strict-typing`, `exception-translations` and `action-exceptions` as done.
- **Reproducible CI**: Home Assistant, `pytest-homeassistant-custom-component`, mypy and ruff are pinned (the mypy job installs the same requirements as the tests).
- **Private Bluetooth access isolated** in `bt_internals.py`: if a Home Assistant release removes the hook for connecting through one proxy, one warning is logged and the normal connection is used; a PIN-bonded sensor whose proxy cannot be verified is refused before anything is sent (new message). Documented in `docs/design-notes.md`.

## 0.23.0-beta.4

Git tag `v0.23.0-beta.4`, manifest version `0.23.0b4`.

- **A stalled link after the end marker is no longer counted as a successful flash**: only a real disconnect (link dropped, `BleakError`, client not connected) means "rebooting"; a read timeout with the link still up ends the flash as unverified, keeps the "flash failed" issue and reads the version back afterwards.
- **"Flash may not have been applied" repair issue** when the version cannot be read back after a flash (all 8 attempts); it clears when the sensor answers again. An unverified flash also clears its failure issue once the expected version is read.
- **Fewer database writes**: the proxy path, its signal strength, the queue position and the last error of the update entity are no longer recorded in the history.
- **A sensor that cannot be read is logged once at INFO** ("is unavailable: reason") and once when it works again; `quality_scale.yaml` corrected (`action-setup` done, `action-exceptions` todo, `strict-typing` todo, polling comment).
- **Stale PIN texts removed**: error and repair texts no longer say that PIN sensors are unsupported; they point to the pairing package or to removing the PIN. The unused `pairing_required` string is gone.

## 0.23.0-beta.3

Git tag `v0.23.0-beta.3`, manifest version `0.23.0b3`.

- **Link loss during a flash** (Bluetooth error while sending blocks or reading the status) is now an ordinary failed flash: the "flash failed" repair issue is created, the entity leaves "installing" and the last-flash record says what happened. Before, the raw error escaped.
- **A setting changed while another change is being written** is no longer dropped: it goes out in the next batch, and the caller sees the real result.
- **Lost PIN bond during a poll, refresh or the post-flash check** is handled like at setup (log line, repair issue or re-pairing, entities unavailable), not as an unhandled error; the post-flash check stops instead of retrying eight times.
- **Adding a sensor by hand** that has a PIN and cannot be paired no longer puts it on the ignored list (only discovery does). New abort message.
- **PIN documentation corrected**: the PIN is stored in the config entry on purpose (redacted in diagnostics); `docs/pin-sensors.md` now explains that the pairing call passes through Home Assistant's event bus (and possibly the recorder) and how to keep it out.
- **Bond cleanup**: removing a PIN sensor asks its proxy to forget the bond. Needs the updated ESPHome package (new `forget_pvvx` action; fully backward compatible, an older package just keeps the bond and the integration logs it).
- **Documentation fix**: the integration does not compute the Telink image CRC (earlier notes said it did); it checks signature, size limit, header length and board, and the sensor verifies the CRC itself.

## 0.23.0-beta.2

Git tag `v0.23.0-beta.2`, manifest version `0.23.0b2`.

- **Quieter logs**: connection, encryption, pairing and per-window flash timing details moved from INFO to DEBUG. INFO keeps one line per meaningful event (flash started/finished, sensor added or paired, clock set, encryption restored, reboot, pairing restored).
- **Documentation**: short README; details moved to `docs/` (usage, PIN sensors, troubleshooting).
- **PIN-protected sensors are now marked experimental**: officially supported are sensors without a PIN; the PIN workaround via the ESPHome package is documented in `docs/pin-sensors.md`, including the known failure case and the TelinkMiFlasher fix.

## 0.23.0-beta.1 (first public beta)

First public release. Git tag `v0.23.0-beta.1`, manifest version `0.23.0b1`. Install it as a pre-release (see the README).

- **Firmware update entity** per sensor: installed version read over Bluetooth, latest from pvvx's `firmware.json`, install with progress, release notes built from pvvx's version history, optional beta firmware.
- **Flashing over any Bluetooth path** Home Assistant has (ESPHome proxies or local adapters), the image chosen for the sensor's own hardware id, checked before sending (board, size, signature, CRC), one operation at a time, queue for *Update all*.
- **Faster, safer flashes**: fast connection parameters requested through the proxy, a slow-link check that stops the flash before the end marker (the sensor keeps its old firmware), a weak-signal warning, a battery check.
- **Sensor settings** (verified firmware 4.2 to 5.9): advertising and measure interval, TX power, display options per board, calibration offsets, clock sync, reboot, refresh; settings are saved before a flash and can be restored afterwards. Most entities are disabled by default.
- **Opt-in dangerous settings**: BTHome encryption and bindkey (`set_encryption`), PIN (`set_pin`), advertising type, PHY, reset to defaults.
- **PIN-protected sensors** through an ESPHome proxy with the `esphome/pvvx_pin_pairing.yaml` package: the config flow asks for the PIN and pairs through the proxy's `pair_pvvx` action; the bond is repaired automatically when the stored PIN is still known.
- Repair issues for failed flashes, lost pairing, encryption not restored, settings that differ after a flash.
- Diagnostics download, English and German translations, quality scale Silver.
