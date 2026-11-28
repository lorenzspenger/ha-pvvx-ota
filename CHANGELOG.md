# Changelog

## Unreleased

- **Automatic clock drift calibration.** On sensors with the time-adjust command, the integration measures how far the clock drifted since its clock was last set (at least 48 hours, at least 5 s, the same method as TelinkMiFlasher) and corrects the sensor's clock tick step, at most once a day. It follows the option *Set the clock automatically*; the last measurement is in the diagnostics.
- **A flash across firmware 5.7/5.8 now also carries over the temperature and humidity calibration (the offsets)** when both firmware versions are 4.8 or newer and the offsets are not the factory values. They were reset by the flash before. After a Home Assistant restart in the window they are still lost.
- **`flash_file` refuses a file above 128 KiB before reading it** with a translated error, instead of loading the whole file first.
- **The battery check only uses a voltage reported within the last 6 hours,** prefers the BTHome voltage sensor if the device has several, and ignores unavailable or unknown values. Without a recent voltage it logs a warning with the age of the last report and flashes anyway; a battery percentage is not converted to volts.
- **Timeouts, lost links, too-slow links and "not a pvvx sensor" now show a fully translated error message** (English and German) instead of a translated prefix with the English reason.
- **The slow-link abort text now fits sensors without a PIN too.** It says to move a proxy closer to the sensor, and for a PIN sensor to pair it through a closer proxy with *Reconfigure*.
- **A silent probe on a PIN-bonded link no longer starts a new pairing.** The sensor is paired again only after an authentication or encryption error, or after two silent probes in a row.
- **New repair issue *paired proxy not available* (`pin_proxy_missing`).** It appears after three failed reads in a row because the proxy that holds the bond no longer sees the sensor, points at *Reconfigure*, and clears on the next successful read or a successful reconfigure.
- **Adding a sensor while a flash or *Update all* runs shows the error "a firmware operation is running" instead of waiting.**
- **Unloading, reloading or removing a sensor while its firmware is being sent stops the transfer before the end marker,** so the sensor keeps its old firmware. After the end marker the transfer finishes and the version check runs at the next setup.
- **While one sensor is flashed, the settings entities, *Restore previous settings* and the buttons of the other sensors show their state correctly** instead of staying stale. Configuration writes are still refused for all sensors during a flash.
- **The *flash may not have been applied* issue clears on any successful read at or above the expected version,** also when the version did not change. A flash to an older version clears it only when that exact version is read.

## 0.26.0-beta.6

Git tag `v0.26.0-beta.6`, manifest version `0.26.0b6`.

- **Breaking: `pvvx_ota.flash_latest` no longer has the `status_every` field**; the status is read after every 8th block.
- **New page `docs/stability.md`, "What stays stable from 1.0":** it lists the stable, experimental and not stable interfaces.
- **`pvvx_ota.set_pin` is marked experimental** in its action name and in the documentation. The diagnostics layout is documented as not stable.
- **Repair issue ids are listed as not stable** in `docs/stability.md`.
- **The `confirm` field of `pvvx_ota.set_pin` is no longer marked required** in the action dialog; the action still refuses unless it is true.
- **Removing the last entry also removes the *pairing package out of date* repair issues.**

## 0.26.0-beta.5

Git tag `v0.26.0-beta.5`, manifest version `0.26.0b5`.

- **The pairing package has a watchdog and a `pvvx_clear_pin` action:** a pairing run that hangs is stopped after 90 s (result `timeout`) and the pairer is free again; the integration retries once, like *no connection*. `pvvx_clear_pin(mac, pin)` is an experimental rescue for a sensor whose pairing is broken, documented in `docs/pin-sensors.md`.
- **The pairing package reports a version** (*pvvx package version* diagnostic sensor). An older or missing version logs one warning per proxy and raises the *pairing package out of date* repair issue, which clears when a later pairing finds the current version.
- **The proxy's pairing entities are found by the name the package gives them**, so renaming them in Home Assistant no longer breaks pairing.
- **The package install snippet shows both `@main` and a release tag** (`@v<release>`).
- **The *firmware not verified* repair issue has its text** (English and German).
- **The PIN pairing steps say "30 to 40 seconds"** instead of "about 25 seconds".
- **The `flash_latest` action description** says what it does: flash the latest release or the named `version`, with `status_every` and `force`.
- **`docs/usage.md` rewritten from the current code.** Corrections in the troubleshooting, PIN sensor, known limitations, supported functions, data update and design notes pages.

## 0.26.0-beta.4

Git tag `v0.26.0-beta.4`, manifest version `0.26.0b4`.

- **Diagnostics hide Bluetooth addresses and user-chosen names:** addresses keep only their last three bytes (`XX:XX:XX:0A:CF:E5`), proxies are `proxy_N`, devices `device_N`, the entry title `sensor` and entity ids `<domain>.<key>`; PINs and keys stay redacted.

## 0.26.0-beta.3

Git tag `v0.26.0-beta.3`, manifest version `0.26.0b3`.

- **The clock read-back after a set and its "clock did not keep the set time" warning are removed.**
- **After a flash the clock is set last**, after the key and settings are written back.

## 0.26.0-beta.2

Git tag `v0.26.0-beta.2`, manifest version `0.26.0b2`.

- **Clock diagnostics:** every clock read is logged at debug level with the sensor's time, its last-set time and the offset; the "clock set" line shows when the sensor was last set and whether it restarted since; diagnostics include the last clock read.

## 0.26.0-beta.1

Git tag `v0.26.0-beta.1`, manifest version `0.26.0b1`.

- **Install a specific firmware version, including an older one:** `update.install` accepts `version` (for example `5.7`), `pvvx_ota.flash_latest` has a new `version` field.
- **Only verified releases 4.2 to 5.9 with an image for the sensor's board are accepted**, and not older than the firmware that knows the sensor's hardware id; a refusal lists the allowed versions.
- **Older images come from pinned pvvx commits** and are checked against a SHA-256 and size table before the flash; from 4.7 on the board byte in the image header is checked too.
- **New script `scripts/generate_firmware_versions.py`** regenerates that table from a clone of pvvx/ATC_MiThermometer.
- **A downgrade across firmware 5.7/5.8 carries over settings and encryption key**, as an update does.
- **An unconfirmed flash counts as done only if the sensor reports exactly the version that was flashed.**
- **New docs:** `docs/downgrade-research.md` (source research behind this).
- **Firmware newer than this integration knows (for example the pvvx 6.0 beta) is marked in the release notes**, and after such a flash the new *firmware not verified* repair issue says that encryption and settings were not restored or checked; it clears once the sensor runs a verified version.
- **Reconfigure pairs a sensor whose PIN was set after it was added:** the *PIN was set* repair issue now leads to *Reconfigure*, which stores the PIN and pairs through a proxy that offers `pair_pvvx`; without such a proxy it stops with a hint. The entry, its options and settings stay.
- **`pvvx_ota.set_pin` keeps the entry in step with the sensor:** on a PIN-bonded entry the new PIN is stored and the sensor is paired again through its proxy (the *pairing lost* issue appears if that fails); clearing the PIN removes the bond from the entry and asks the proxy to forget it. The response of a bonded entry carries `paired`.
- **The post-flash version check survives a restart or reload:** a restart, entry reload or integration update within a few minutes after a flash now runs the check afterwards (encryption restored, differing settings flagged, installed version updated; a flash across firmware 5.7/5.8 raises the *encryption key replaced* issue).

## 0.26.0-dev.1

Git tag `v0.26.0-dev.1`, manifest version `0.26.0.dev1`. Test build from the `feat/specific-version` branch; not for general use.

- **Install a specific firmware version, including an older one:** `update.install` accepts `version` (for example `5.7`), `pvvx_ota.flash_latest` has a new `version` field.
- **Only verified releases 4.2 to 5.9 with an image for the sensor's board are accepted**, and not older than the firmware that knows the sensor's hardware id; a refusal lists the allowed versions.
- **Older images come from pinned pvvx commits** and are checked against a SHA-256 and size table before the flash; from 4.7 on the board byte in the image header is checked too.
- **New script `scripts/generate_firmware_versions.py`** regenerates that table from a clone of pvvx/ATC_MiThermometer.
- **A downgrade across firmware 5.7/5.8 carries over settings and encryption key**, as an update does.
- **An unconfirmed flash counts as done only if the sensor reports exactly the version that was flashed.**
- **New docs:** `docs/downgrade-research.md` (source research behind this).

## 0.25.0-beta.7

Git tag `v0.25.0-beta.7`, manifest version `0.25.0b7`.

- **Breaking for automations that use the old select option text:** the five select entities now use stable option ids and show translated texts. Advertising type: `ATC1441`, `pvvx custom`, `Mi`, `BTHome` -> `atc1441`, `pvvx`, `mi`, `bthome`. Temperature unit: `°C`, `°F` -> `celsius`, `fahrenheit`. Smiley: `frame off` ... `frame ooo` -> `frame_off`, `frame_happy`, `frame_sad`, `frame_ooo` (`off`, `happy`, `sad`, `ooo` unchanged). Display content: `battery %`, `battery V` -> `battery_percent`, `battery_voltage` (others unchanged). TX power: `+3.01 dBm` -> `dbm_p3_01`, `-3.03 dBm` -> `dbm_m3_03` (same pattern for every level). An unlisted value: `raw N` -> `raw_N`.

## 0.25.0-beta.6

Git tag `v0.25.0-beta.6`, manifest version `0.25.0b6`.

- **The sensor's clock is also set in the periodic version read** and in the check after a flash.
- **A failed or unconfirmed flash is logged as a warning** by the integration.

## 0.25.0-beta.5

Git tag `v0.25.0-beta.5`, manifest version `0.25.0b5`.

- **A flash across firmware 5.7/5.8 (either direction) keeps encryption working**: the key is carried over in memory and written back after the version check.
- **The saved settings are written back automatically after such a flash.**
- **New repair issue "the flash replaced the encryption key"** when the key could not be carried over; `set_encryption` with the old key (or a new one) clears it.
- **`keep_key` is refused while that issue is open.**
- **Firmware 5.6 is now verified** and gets the full settings.
- **"Advert encryption changed" no longer clears on a signal strength or diagnostic reading** of the BTHome entry.

## 0.25.0-beta.4

Git tag `v0.25.0-beta.4`, manifest version `0.25.0b4`.

- **`pvvx_ota.set_encryption` has a new `keep_key` option** that switches encryption on with the key the sensor still holds, without needing to know it.

## 0.25.0-beta.3

Git tag `v0.25.0-beta.3`, manifest version `0.25.0b3`.

- **Repair issues now stay after a Home Assistant restart.**

## 0.25.0-beta.2

Git tag `v0.25.0-beta.2`, manifest version `0.25.0b2`.

- **"Advert encryption changed" is a repair issue again**; it clears when Home Assistant receives the next reading from the sensor through BTHome.
- **"Settings were reset to defaults" is a repair issue again**; its dialog restores the previous settings, and the issue clears once the sensor's settings match the saved ones again (after restoring here or with the *Restore previous settings* button, or after a fix with another tool).
- **`set_encryption` raises the "advert encryption changed" issue only when the encryption flag or the key actually changes.**
- **"Restore previous settings" also switches the encryption flag back on** when it was on and the sensor still holds its key; otherwise the "encryption was not switched back on" issue is raised.
- **Restore previous settings and the repair dialog also bring encryption back** when a reset changed only the encryption flag.
- **"Advert encryption changed" also clears when the BTHome entry is added again** after the issue was raised.
- **"Settings were reset to defaults" is not raised when the sensor already had default settings.**
- **The "encryption was not switched back on" text also covers a restore after a reset.**

## 0.25.0-beta.1

Git tag `v0.25.0-beta.1`, manifest version `0.25.0b1`.

- **"Advert encryption changed" and "settings were reset to defaults" are notifications instead of repair issues**; existing ones are removed.
- **Switching encryption on with `set_encryption` clears the "encryption was not switched back on" repair issue.**
- **Repair issues clear themselves once a read shows the cause is gone**, also when it was fixed with another tool: advertising type not BTHome, encryption not switched back on, a PIN that was set, settings that differ after a flash.
- **The "firmware update failed" and "flash may not have been applied" issues also clear when a version read shows the latest firmware.**

## 0.24.0

Git tag `v0.24.0`, manifest version `0.24.0`. Contains everything from 0.24.0-beta.1 and beta.2, plus:

- **Clearer "could not connect" message when adding a sensor**: it no longer rules out a PIN and suggests removing the PIN if it keeps failing.

## 0.24.0-beta.2

Git tag `v0.24.0-beta.2`, manifest version `0.24.0b2`.

- **Icons for every entity and action**, shown in the entity list and the action picker.
- **Device classes**: the Reflash button is shown as an update button and the advertising interval as a duration.
- **A device created by the integration shows the installed firmware version** (when no BTHome device exists).
- **New documentation pages**: use cases, supported devices, supported functions, how data is updated, known limitations and examples (linked from the README).
- **Quality scale file lists every rule up to Platinum**, each marked done or exempt.

## 0.24.0-beta.1

Git tag `v0.24.0-beta.1`, manifest version `0.24.0b1`.

- **The firmware list is stored**: the last good list and release notes are kept across restarts and used while GitHub cannot be reached (update offers, board check, `flash_file`).
- **Diagnostics show where the firmware list comes from** (live or stored, fetch time, age); the log says once when the stored copy is in use.
- **`flash_file` has a new option `skip_board_check`** (off by default) that skips only the board check; every other check still applies and each use is logged as a warning.
- **`flash_file` without a firmware list is refused** with an error that names `skip_board_check`.

## 0.23.0

First stable release. Git tag `v0.23.0`, manifest version `0.23.0`.

- **Faster post-flash check**: no connection to the sensor for 30 s after a flash, then the new version is read after 30 s and retried every 30 s (8 attempts).
- **A normal reboot after a flash is no longer reported as "unverified"**.
- **Reflash button text**: a flash takes about 1.5 to 3 minutes on a good link.

## 0.23.0-beta.6

Git tag `v0.23.0-beta.6`, manifest version `0.23.0b6`.

- **A newer flash replaces the waiting post-flash version check**; unloading cancels it.
- **Internal cleanup**: no behaviour change, tests and CI dependencies pinned.

## 0.23.0-beta.5

Git tag `v0.23.0-beta.5`, manifest version `0.23.0b5`.

- **Translated error messages** (English and German).
- **Invalid input is reported as such**: a bad key or PIN, a file path that is not allowed or unreadable, a low battery without `force` and a missing `confirm`.
- **A PIN-bonded sensor is refused** when the proxy its connection runs through cannot be verified; nothing is sent to it.
- **Internal cleanup**: strict typing, shared state in its own module, reproducible CI.

## 0.23.0-beta.4

Git tag `v0.23.0-beta.4`, manifest version `0.23.0b4`.

- **A stalled link after the end marker is not counted as a successful flash**: the flash ends as unverified, the failure issue stays and the version is read back.
- **New repair issue "flash may not have been applied"** when the version cannot be read back after a flash.
- **Fewer database writes**: the proxy path, signal strength, queue position and last error of the update entity are no longer recorded.
- **Unavailable sensors are logged once** when they become unreachable and once when they are back.
- **Error and repair texts** no longer say that PIN sensors are unsupported.

## 0.23.0-beta.3

Git tag `v0.23.0-beta.3`, manifest version `0.23.0b3`.

- **Link loss during a flash** is reported as a failed flash with the "flash failed" repair issue.
- **A setting changed while another change is being written** is no longer dropped.
- **A lost PIN bond** during a poll, refresh or the post-flash check is handled like at setup (log line, repair issue or re-pairing).
- **Adding a PIN sensor by hand** that cannot be paired no longer puts it on the ignored list (new abort message).
- **Removing a PIN sensor asks its proxy to forget the bond** (needs the updated ESPHome package with the new `forget_pvvx` action; older packages keep the bond).
- **Documentation**: the PIN is stored in the config entry (redacted in diagnostics) and passes through Home Assistant's event bus; the integration does not compute the image CRC, the sensor verifies it.

## 0.23.0-beta.2

Git tag `v0.23.0-beta.2`, manifest version `0.23.0b2`.

- **Quieter logs**: connection, encryption, pairing and flash timing details moved from INFO to DEBUG.
- **Shorter README**; details moved to `docs/`.
- **PIN-protected sensors are marked experimental**.

## 0.23.0-beta.1 (first public beta)

Git tag `v0.23.0-beta.1`, manifest version `0.23.0b1`. Install it as a pre-release (see the README).

- **Firmware update entity** per sensor: installed version read over Bluetooth, latest from pvvx's `firmware.json`, install with progress, release notes, optional beta firmware.
- **Flashing over any Bluetooth path** Home Assistant has (ESPHome proxies or local adapters): the image is chosen for the sensor's hardware id and checked before sending (board, size, signature), one operation at a time, queue for *Update all*.
- **Safer flashes**: fast connection parameters requested through the proxy, a slow-link check that stops the flash before the end marker, a weak-signal warning, a battery check.
- **Sensor settings** (verified firmware 4.2 to 5.9): advertising and measure interval, TX power, display options per board, calibration offsets, clock sync, reboot, refresh; settings are saved before a flash and can be restored afterwards. Most entities are disabled by default.
- **Opt-in dangerous settings**: BTHome encryption and bindkey (`set_encryption`), PIN (`set_pin`), advertising type, PHY, reset to defaults.
- **PIN-protected sensors** through an ESPHome proxy with the `esphome/pvvx_pin_pairing.yaml` package: the config flow asks for the PIN and pairs through the proxy; the bond is repaired automatically when the stored PIN is known.
- **Repair issues** for failed flashes, lost pairing, encryption not restored and settings that differ after a flash.
- **Diagnostics download**, English and German translations.
