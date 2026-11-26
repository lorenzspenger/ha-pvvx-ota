# Changelog

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
