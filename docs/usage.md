# Usage

Everything the integration offers and how it behaves. Back to the [README](../README.md).

## What it does

The integration updates and configures [pvvx ATC_MiThermometer](https://github.com/pvvx/ATC_MiThermometer) custom firmware over Home Assistant's own Bluetooth stack, through ESPHome Bluetooth proxies or a local adapter. Every sensor is one config entry. The sensor's readings stay with Home Assistant's BTHome integration; this integration only reads the firmware version and the configuration, writes settings and flashes firmware. It keeps no connection open and never polls the readings.

* One **update entity** per sensor: installed version read over Bluetooth, latest version from pvvx's [`firmware.json`](https://github.com/pvvx/ATC_MiThermometer/blob/master/firmware.json), install with progress, release notes, an optional beta and the choice of a specific version.
* **Settings entities** (intervals, TX power, display options, calibration offsets, clock sync, reboot), most of them disabled by default, with the settings saved before every flash.
* **Actions** to flash a version or a local file and to set the advert encryption and the PIN.
* **Repair issues** when something needs your attention, and a redacted diagnostics download.

## Requirements

* Home Assistant 2026.10.0 or newer.
* An ESPHome Bluetooth proxy with active connections (ESPHome 2026.10 or newer is recommended for fast flashes) or a local adapter that can connect. Keep the sensor close to the proxy: better than about -75 dBm.
* pvvx custom firmware already on the sensor. The first flash from stock firmware needs the [web flasher](https://pvvx.github.io/ATC_MiThermometer/TelinkMiFlasher.html).
* Firmware 4.2 to 5.9 gets the full settings ([version-layouts.md](version-layouts.md)); other versions get the update entity only. Boards: [supported-devices.md](supported-devices.md) and [display-layouts.md](display-layouts.md).

## Adding a sensor

*Settings → Devices & services → Add integration → pvvx Thermometer OTA*, or accept a discovered sensor. Discovery is only a hint and never connects: it matches the name `ATC_*`, service data 0x181A (pvvx custom format) and 0xFCD2 (BTHome v2). The manual picker lists every sensor in range that advertises one of these.

The confirm step connects once and reads the sensor's configuration block. If the sensor does not answer like a pvvx device the flow aborts; otherwise the board id and the firmware version are stored in the entry. Setting up an entry later (also at every Home Assistant start) makes no Bluetooth connection: it checks that a connectable scanner heard the sensor lately and creates the entities from the stored board and version.

The update and configuration entities are attached to the sensor's existing BTHome device (found by its Bluetooth address), so nothing on that device is renamed or moved. Without a BTHome device the integration creates a device of its own and moves the entities when a BTHome device appears.

A sensor with a **PIN** answers only over an encrypted link. It needs an ESPHome proxy with the pairing package, which pairs once and keeps the bond. The flow asks for the PIN, stores it in the entry (redacted in diagnostics, never logged) and pairs again by itself when the bond is lost. Details, the package and the limits: [pin-sensors.md](pin-sensors.md). A sensor with a PIN and no such proxy is ignored with a hint.

## Entities

All entities sit on the sensor's device, in the *Configuration* category (the update entity and the hardware version sensor excepted). **Only two are enabled by default: the update entity and the *Restore previous settings* button.** Everything else is registered but disabled: enable what you need under *Settings → Devices & services → pvvx Thermometer OTA → entities (show disabled)* → entity → *Settings → Enabled*. The default applies when an entity is first created; existing entities keep their state.

Which settings entities exist depends on the firmware version and board stored in the entry. Without a verified firmware (4.2 to 5.9) only the update entity, *Refresh configuration*, *Reflash firmware*, *Reboot sensor*, *Sync clock* and *Beta firmware* exist: no settings entities and no *Restore* or *Reset* buttons.

| Entity | Platform | Default | What it does |
|---|---|---|---|
| Firmware | update | on | Installed and latest version, install with progress, release notes, specific version |
| Restore previous settings | button | on | Writes back the settings saved before the last flash or reset, and switches the encryption flag back on if it was on and the key is still on the sensor. Available while the settings differ from the saved ones. Needs no other entity |
| Refresh configuration | button | off | Reads the configuration again (and sets the clock if automatic clock sync is on). The way to fill the settings after a failed read |
| Reflash firmware | button | off | Flashes the latest release (or the beta if the beta switch is on) even when the sensor is up to date, through the same path as *Install*. Unavailable while a flash runs |
| Reboot sensor | button | off | Sends the reboot command (`0x72`, reboot on disconnect); the sensor restarts in about 2 s and keeps its settings. Needs the sensor to confirm the command |
| Sync clock | button | off | Sets the sensor's clock to local time (`0x23`, the payload of TelinkMiFlasher's "Set Time") |
| Reset settings to defaults | button | off | Factory values for the configuration block, advertising type and encryption flag included. The settings before are saved first |
| Beta firmware | switch | off | Offer pvvx's beta instead of the stable release; kept in the entry options |
| Advertising interval | number | off | 0.0625 to 10 s in steps of 0.0625 s |
| Measure interval | number | off | A measurement every N advertising intervals, 2 to 25 |
| Temperature offset, Humidity offset | number | off | Calibration offsets, -12.5 to 12.5 in steps of 0.1 (°C and %). Firmware 4.8 or newer and a chip with known factory values. Relative to the chip's factory value; slope factors stay untouched and the sensor's reply is checked. Change only after comparing with a reference |
| TX power | select | off | A subset of the firmware's levels; a current value that is not listed shows as `raw_N` and is never changed |
| Advertising type | select | off | See *Settings that can lock you out* |
| Long range PHY, BT5 PHY | switch | off | See *Settings that can lock you out* |
| Hardware version id | sensor | off | The hardware version byte of the configuration block (diagnostic, read only) |

Display entities depend on the board (hardware id) and are all disabled by default:

| Boards | Entities |
|---|---|
| LYWSD03MMC family (0, 3, 4, 5, 10, 14) | Display temperature unit and Smiley (selects), Comfort smiley, Show battery on display and Screen off (switches) |
| MJWSD05MMC (9, 12) | Display temperature unit and Display content (selects), Comfort smiley, Two decimals on display, 12-hour clock, Show weekday and Screen off (switches); Date as DD/MM on the English variant (12) only |
| MHO-C401 (1, 8), CGG1 (2, 7), CGDK2 (6), MHO-C122 (11), MJWSD06MMC (13) | Display temperature unit and Screen off. On e-ink boards screen off freezes the last picture |
| Other boards (LYWSD02MMC, TS0201, ...) | no display entities |

Older firmware lacks a few bits: *Screen off* needs 4.3 or newer, *Show weekday* 4.9 and *Date as DD/MM* 5.4. The meaning of each bit per board is in [display-layouts.md](display-layouts.md).

**Select option ids.** The state of every select is a stable lowercase id; Home Assistant shows the translated text (English and German). Use the id in automations; the ids do not change between releases.

| Select | Ids |
|---|---|
| Advertising type | `atc1441`, `pvvx`, `mi`, `bthome` |
| Display temperature unit | `celsius`, `fahrenheit` |
| Smiley | `off`, `happy`, `sad`, `ooo`, `frame_off`, `frame_happy`, `frame_sad`, `frame_ooo` |
| Display content | `time`, `temperature`, `humidity`, `battery_percent`, `battery_voltage` (the sensor can also report `external`, which cannot be selected) |
| TX power | `dbm_p10_46`, `dbm_p7_79`, `dbm_p5_13`, `dbm_p3_01`, `dbm_p0_04`, `dbm_m3_03`, `dbm_m9_89`, `dbm_m15_88`, `dbm_m25_18` (p = plus, m = minus, the digits are the dBm value), or `raw_N` for an unlisted current value |

**How the settings are read and written.** The first read runs in the background after setup: at once after a reload or a new entry, otherwise after Home Assistant has started, with the entries 4 s apart. It is skipped while no entity other than the update entity and *Restore previous settings* is enabled. Until it succeeds the settings entities are *unavailable*. A failed read is retried after 5, 15 and 60 minutes; after that the next version read (see the options) or *Refresh configuration* fills them in. A sensor that refuses the link in that read (a PIN was set later, or the bond was lost) is not retried; a repair issue explains it. Nothing is polled afterwards.

A write reads the configuration block fresh inside its own connection, changes only the targeted bits and bytes, sends everything else back unchanged and compares the sensor's answer with what was requested (you get an error naming the stored value if the firmware clamped it). Changes arriving within 2 s share one connection. Nothing is written while a flash runs, and one lock makes sure that only one Bluetooth operation runs at a time across the whole integration. Not exposed: `adv_flags`, connect latency, averaging, the pseudo-random advertising delay and the history. The layout was compared with the firmware sources for 4.2 to 5.9 only, and writes are refused on any other version.

## Updating firmware

### Normal update

The update entity offers the latest release listed in pvvx's `firmware.json` for the sensor's own hardware id (the lookup pvvx's web flasher uses). The list and the release notes are fetched at startup and every 12 hours over HTTPS without Bluetooth, are cached for 6 hours and never fetched more often than every 10 minutes. The last good copy is also kept in Home Assistant's storage and used when GitHub cannot be reached: offers, the board check and `flash_file` work from it, a download of an image that is not cached still fails. The installed version is read over Bluetooth (see the options). *Install* shows the progress. The release notes dialog and `release_summary` are built from the version history table in pvvx's README for the versions between the installed and the offered one; if the README cannot be read you get a link.

Attributes of the update entity: `hardware_id`, `board`, `beta_version`, `beta_firmware_option`, `beta_status` (why a requested beta is not used), `last_error` (the last failed version read), `expected_proxy` and `expected_proxy_rssi` (the proxy and signal the next connection will probably use) and `queued_position`.

### Beta firmware

With the *Beta firmware* switch on and a beta in `firmware.json` (`betaver` plus a `betafw` list, often absent) that has an image for the sensor's hardware id, the update entity offers the beta (`latest_version` is `betaver`, the release summary says so). Otherwise it stays on stable and `beta_status` says why. A beta is offered only when it is newer than the stable release, so the switch never causes a downgrade. The beta image gets the same checks as a stable one (same board, within the BLE OTA size limit, Telink `KNLT` signature, header length); `betafw` entries must be on `raw.githubusercontent.com/pvvx/...` or `github.com/pvvx/...`.

### A specific version (also a downgrade)

The update entity accepts a version: the *Install* dialog has a version field, or call the action:

```yaml
action: update.install
target:
  entity_id: update.thermometer_firmware
data:
  version: "5.7"
```

`pvvx_ota.flash_latest` takes the same `version`. Without a version, or with the version that is currently offered, the latest release is flashed as usual. Before anything is sent to the sensor:

* The version must be one of the verified releases, 4.2 to 5.9, with an image for the sensor's board, and that release must already know the sensor's hardware id (later boards need newer releases: the 2025 LYWSD03MMC, hardware id 14, needs 5.3). Anything else is refused with a message that lists the allowed versions. The installed version may be chosen again.
* The image is not taken by file name from pvvx's `master` (the names are reused for rebuilds). A table of release commits with the SHA-256 and size of every image (`firmware_versions.py`, generated by `scripts/generate_firmware_versions.py`) pins the bytes: the image is downloaded from that commit and the flash is refused if they differ. The board byte of the image header is checked where the table has one.
* Size, signature, battery check and queue work as for any flash.

A flash across firmware 5.7/5.8 in either direction is carried over as described under *After a flash*. After a downgrade below 4.7 read the settings again before writing them. Downgrades are derived from the firmware source and untested on hardware: read [known-limitations.md](known-limitations.md) first.

### Firmware newer than the integration knows

If `firmware.json` offers a version above 5.9, the release summary and the release notes say that after the flash encryption and settings are not restored or checked and the settings entities are unavailable. The flash itself works as for any version.

### Battery check

Before a flash the integration reads the battery voltage from the voltage sensor of the sensor's BTHome device (no extra Bluetooth traffic) and refuses below the limit (default 2.5 V, an option) with a clear message. With no voltage known it logs a warning and flashes anyway. `flash_latest` and `flash_file` accept `force: true` to flash anyway; *Install* and *Reflash firmware* have no override, lower the limit in the options instead.

### Link quality

Before a flash the proxy it will probably use is looked up. If that proxy hears the sensor below -75 dBm a warning is logged and added to the error text should the flash fail; a flash is never refused for this. The transfer itself is judged on its first seconds: below 15 blocks per second (a normal link does 60 and more) the fast connection parameters are requested once more and the rate is measured again; if it is still that slow the flash is stopped before the end marker, the sensor keeps its old firmware and the failure names the rate, the proxy and the signal. More in [troubleshooting.md](troubleshooting.md).

### Queue and *Update all*

Firmware operations (*Install*, *Reflash firmware*, `flash_latest`, `flash_file`) run one at a time across all sensors. A second one is queued instead of refused: the update entity shows `in_progress` without a percentage and a `queued_position`, first come first served, for up to 2 hours. *Settings → Updates → Update all* therefore flashes the sensors one after another. Unloading an entry cancels its queued job.

### A local file

`pvvx_ota.flash_file` flashes a local `.bin`. The path must be allowed by `allowlist_external_dirs`, and the file must be named `<board>_v<NN>.bin` for the sensor's board (for example `MHO_C401_v59.bin`; `MHO_C401N_*` is refused for an `MHO_C401`). The board is derived from the firmware list (live or stored copy); without a list the action refuses. With `skip_board_check: true` only that check is skipped (no list is needed, so it also works offline): signature, size and header checks, the battery check, the slow-link stop and everything else still apply, and a WARNING is logged. A wrong image can stop a sensor from working. *Install*, *Reflash firmware* and *Update all* never skip the check.

## After a flash

Before every flash the integration saves the sensor's settings and whether BTHome encryption was on. After the transfer:

1. **Quiet period.** No other connection is opened to the sensor for 30 s (it is rebooting, which takes a few seconds), then the version check runs: up to 8 attempts, 30 s apart. The check is remembered in the entry data while it is outstanding. If Home Assistant restarts or the entry reloads in that window it runs again after the restart (a few seconds after setup at the earliest; dropped if the flash is more than 2 hours old). A carried key does not survive a restart, see step 3.
2. **Version check.** Once the new version is read, the failed-flash issues clear and the steps below run. If the version cannot be read in 8 attempts the installed version stays unchanged and the *flash may not have been applied* issue is raised. A flash that ended with a stalled link after the end marker counts as unconfirmed and is decided by the same check.
3. **Across firmware 5.7/5.8 (either direction)** the sensor erases everything it stored, encryption key included, and starts with defaults and a new random key. The integration reads the key before such a flash (only if encryption is on) and keeps it in memory only. After the version check it writes the key back, switches encryption on and writes the saved settings back, without any action from you; the clock is set in the last connection of that sequence. If this fails (Home Assistant restarted meanwhile, the sensor did not accept the key, or the option *Switch BTHome encryption back on after a flash* is off) encryption stays off and the *the flash replaced the encryption key* issue explains the fix. The **PIN** and the **temperature/humidity calibration coefficients** are erased as well and are not carried over: set them again (a PIN sensor: [pin-sensors.md](pin-sensors.md)).
4. **On the same side of 5.8** the BTHome encryption flag is switched back on if it was on before (the option above, default on) and the sensor still holds its key; the key itself is never read out, written or logged. Other settings are not re-applied automatically: if they differ from before the flash the *settings differ from before the flash* issue points at *Restore previous settings*.
5. **Unverified newer firmware.** If the flash ended on a version the integration has not verified, no key, flag or setting is touched or checked and the *firmware not verified* issue is raised.

*Restore previous settings* (and the repair flow of the reset issue) writes only the fields that differ, switches the encryption flag on without writing a key, and is never pressed automatically.

## Clock

The *Sync clock* button always sets the sensor's clock to local time. The integration also checks the clock in connections it makes anyway (the first read, the version read, *Refresh configuration*, the read before a flash, *Reboot sensor* and the last connection of the post-flash write-back) and sets it only if it is more than 5 s off, so there are no extra connections. Failures never affect the connection. The option *Set the clock automatically* switches the automatic part off.

## Actions

All actions are admin only and target the sensor's update entity. Action names, fields and response keys are part of what stays stable from 1.0, see [stability.md](stability.md); `set_pin` is experimental.

| Action | Fields | What it does |
|---|---|---|
| `pvvx_ota.flash_latest` | `version` (optional, for example `"5.7"`), `force` (default false) | Flashes the latest release even if up to date, or the named one |
| `pvvx_ota.flash_file` | `path` (required), `force` (default false), `skip_board_check` (default false) | Flashes a local file, see *A local file* |
| `pvvx_ota.set_encryption` | `enable` (required), `key` (optional, 32 hex characters), `keep_key` (default false) | See below |
| `pvvx_ota.set_pin` | `pin` (0 to 999999, 0 = off), `confirm` (must be true) | See below |

**`set_encryption`.** With `enable` it stores a key (generated if you give none) and switches encrypted adverts on; with `enable: false` it switches them off. The **key is returned only in the action's response** (Developer tools → Actions shows it) and is never put in an entity, attribute or log. Home Assistant's BTHome entry for the sensor then needs that key; this integration does not touch it. With `keep_key: true` (and no `key`) only the flag is switched on and the key the sensor still holds stays as it is (nothing is returned); it fails if the sensor holds no key and is refused while the *the flash replaced the encryption key* issue is open. A change raises the *advert encryption changed* issue.

**`set_pin`.** Refuses unless `confirm: true`. A PIN needs the proxy bond described in [pin-sensors.md](pin-sensors.md); setting or changing the PIN erases the sensor's bonds. On a PIN-bonded entry the new PIN is stored and the sensor is paired again through its proxy; clearing the PIN (0) removes the bond from the entry and the proxy. On an entry without a bond the *PIN was set* issue appears until you *Reconfigure* the entry. The PIN is write only: never read back or logged, and stored only in the entry of a bonded sensor.

Values passed to actions can be visible in Home Assistant's event bus and in automation traces: call these two actions from Developer tools, and do not enable debug logging for `bluetooth`, `habluetooth` or `bleak_esphome` while setting a key or PIN.

## Settings that can lock you out

These can make a sensor disappear from Home Assistant or lock out this integration. Every one is opt-in: the entities are disabled by default and the actions are admin only.

| What | How | Consequence |
|---|---|---|
| Advertising type | Select, disabled | Anything but BTHome stops Home Assistant's BTHome integration from receiving the sensor. The *advertising type is not BTHome* issue stays until a read shows BTHome again |
| Long range PHY, BT5 PHY | Switches, disabled | ESP32 proxies may not receive coded-PHY or BT5 adverts: the sensor can vanish until it is power cycled (long range resets on power-off) |
| Encryption and bindkey | `pvvx_ota.set_encryption` | See *Actions* |
| PIN | `pvvx_ota.set_pin` | See *Actions* |
| Reset settings to defaults | Button, disabled | Resets advertising type and the encryption flag too, so the BTHome link may break. The settings before are saved and *Restore previous settings* offers them again (the encryption flag too, if it was on and the key is still there). The *settings were reset to defaults* issue offers the same restore with *Submit* |

## Options

*Settings → Devices & services → pvvx Thermometer OTA →* the sensor *→ Configure*:

| Option | Default | Meaning |
|---|---|---|
| Minimum battery voltage for a flash | 2.5 V (range 1.8 to 3.2, step 0.05) | A flash is refused below this voltage unless `force` is set |
| Set the clock automatically | on | Check and set the clock in connections made anyway |
| Switch BTHome encryption back on after a flash | on | The flag restore after a flash, and the key write-back across 5.7/5.8 |
| How often to read the sensor's version | every 3 days | Every day, every 3 days, every 7 days or never. Never means only on demand (*Refresh configuration*, `homeassistant.update_entity`), after a flash and in the first read after a start while a settings entity is enabled. The firmware list and the update offers are refreshed separately every 12 hours without Bluetooth, so offers do not depend on this. Applies as soon as you save it |

The *Beta firmware* switch is also an entry option; it keeps its value while the switch entity is disabled.

## Repair issues

Each issue belongs to one sensor and survives a restart.

| Issue | Appears when | Clears when |
|---|---|---|
| Firmware update failed (`flash_failed`) | A flash ended with an error; the old firmware keeps running | After the next successful flash, or when a version read shows the latest firmware |
| Flash may not have been applied (`flash_unconfirmed`) | The image was sent but the new version could not be read in 8 attempts | A later read shows the expected version or a newer one, whether or not the version changed (a flash to an older version needs exactly that version) |
| Firmware not verified (`firmware_unverified`) | A flash ended on a version above 5.9 or otherwise not verified | A read shows a verified version |
| The flash replaced the encryption key (`encryption_key_replaced`) | A flash across 5.7/5.8 could not carry the key over | `set_encryption` with `enable` has switched encryption on (with your old key, or a new one that you enter in the BTHome entry); `keep_key` is refused meanwhile |
| Encryption was not switched back on (`encryption_restore_failed`) | The flag could not be set again after a flash or a restore | A read shows encryption on, however it was switched on |
| Advert encryption changed (`encryption_changed`) | `set_encryption` changed the state | Home Assistant receives a reading from the sensor through BTHome again; if it stays, the BTHome entry's bindkey does not match |
| Advertising type is not BTHome (`adv_type_changed`) | A write set another advertising type | A read shows BTHome |
| Settings differ from before the flash (`settings_differ_after_flash`) | After a flash the settings differ from the saved ones | The settings match again (*Restore previous settings*) |
| Settings were reset to defaults (`reset_defaults`) | *Reset settings to defaults* changed the sensor | *Submit* in the issue (or the restore button) brings the saved settings back, or a read shows them |
| A PIN was set (`pin_set`) | The sensor refuses the link on an entry without a bond | *Reconfigure* pairs it, or the sensor can be read without a PIN again |
| Pairing lost (`pin_bond_lost`) | A bonded sensor refuses even after pairing again with the stored PIN, or the pairing after `set_pin` failed | The next successful read. Fix it with *Reconfigure* (enter the PIN again) or the pair button of a hand-made proxy setup |
| Paired proxy not available (`pin_proxy_missing`) | Three reads in a row failed because the proxy that holds the bond no longer sees the sensor (removed, renamed, replaced or offline) | The next successful read, or a successful *Reconfigure*, which pairs through another proxy |

While a sensor is being added, a failed pairing leaves a *could not be paired with its PIN* notice (`pin_pairing_failed`) that disappears once the sensor has been added. Deleting an entry removes all its issues. Fix steps for the PIN issues: [pin-sensors.md](pin-sensors.md) and [troubleshooting.md](troubleshooting.md).

## Diagnostics

*Download diagnostics* on a sensor's entry gives the entry data and options, the decoded configuration block (it holds no key), the capabilities, the firmware list summary (`live` or `stored`, when fetched, age), the last clock read, the timings and result of the last flash, the proxy the last connection used, the saved settings snapshot, the devices and the entity ids. Keys, PINs and passkeys are redacted, Bluetooth addresses keep only their last three bytes (`XX:XX:XX:0A:CF:E5`) and user-chosen names are replaced: proxies are `proxy_1`, `proxy_2`, ... the same way everywhere in one file. The layout of the file is not a stable interface and can change in any version; do not build automations or tools on it.

## Timeouts and limits

| What | Value |
|---|---|
| Connect, all retries and proxies | 25 s |
| One GATT write or configuration command | 10 s |
| Whole flash | 90 minutes |
| Image size | at most 128 KiB (BLE OTA); larger images need the web flasher |
| Data blocks | 16 bytes, write without response, a status read every 8 blocks |
| Slow-link stop | below 15 blocks/s, judged after the first 3 s of data |
| After the end marker | the sensor must drop the link; a link that stays up and silent for 10 s ends the flash as unconfirmed, a sensor that keeps answering with status 0 for about 8 s did not reboot |
| Quiet period after a flash or a reboot | 30 s |
| Post-flash version check | after 30 s, 8 attempts, 30 s apart |
| Queue wait | 2 hours |
| Settings writes | batched within 2 s |
| Automatic clock tolerance | 5 s |
| First read retries | after 5, 15 and 60 minutes |
| Firmware list | cached 6 hours, refreshed every 12 hours, at most every 10 minutes; while there is none it is retried after 1, 5 and 15 minutes |

## Removal

* **One sensor:** *Settings → Devices & services → pvvx Thermometer OTA →* the sensor's entry *→ ⋮ → Delete*. The integration's entities and issues for that sensor disappear. Nothing is changed on the sensor, and the BTHome entry, device, name, area and entities are never touched. A sensor that was ignored because of a PIN is removed the same way (*Ignored* section, *Delete*) and is then offered again.
* **The integration:** delete every entry, remove automations that use the `pvvx_ota.*` actions, then (HACS) *Remove*, or delete `custom_components/pvvx_ota` by hand, and restart Home Assistant.

## Languages

The interface is available in English and German. Select values are stable ids (see above), so automations keep working in either language.

See also [examples.md](examples.md), [data-updates.md](data-updates.md), [known-limitations.md](known-limitations.md) and [design-notes.md](design-notes.md).
