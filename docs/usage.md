# Usage

Everything the integration offers, in detail. Back to the [README](../README.md).

## Features

* One config entry per sensor (discovered via Bluetooth, or picked manually), with a confirm step.
* One `update` entity per sensor: installed version read from the device, latest from pvvx's [`firmware.json`](https://github.com/pvvx/ATC_MiThermometer/blob/master/firmware.json) (cached 6 h, never fetched more than every 10 min).
* **Install** flashes the image for the device's *own* hardware id (same lookup as pvvx's web flasher), with a progress bar.
* Sensor settings as entities (advertising interval, TX power, display options, calibration offsets, clock sync, reboot), most of them **disabled by default**, with saved settings you can restore after a flash.
* Actions: `pvvx_ota.flash_latest`, `pvvx_ota.flash_file` (a local `.bin`, must be under `allowlist_external_dirs`, admin only), `pvvx_ota.set_encryption` and `pvvx_ota.set_pin` (both opt-in, admin only). The file for `flash_file` must be named exactly `<board>_v<NN>.bin` for the device's board (e.g. `MHO_C401_v59.bin`); `MHO_C401N_*` is refused for an `MHO_C401`.

## Supported boards and firmware

* **Firmware versions** whose settings layout was verified against pvvx's sources: **4.2 to 5.5 (every tag), 5.7, 5.8 and 5.9** ([details](version-layouts.md)). Firmware 4.1 and older, 5.6 (beta commits only) and anything newer than 5.9 get the update entity but **no settings entities** and no write access to the config block.
* **Boards** (pvvx hardware id): LYWSD03MMC family (0, 3, 4, 5, 10, 14), MJWSD05MMC (9, 12), MHO-C401 (1, 8), CGG1 (2, 7), CGDK2 (6), MHO-C122 (11), MJWSD06MMC (13), TS0201 (17). Which display options exist per board: [display-layouts.md](display-layouts.md). Mostly tested on LYWSD03MMC and MJWSD05MMC; other boards follow pvvx's sources but have seen little hardware testing.
* Flashing uses whatever image pvvx's `firmware.json` lists for the sensor's hardware id.

## Sensor settings (configuration entities)

Besides the update entity, each sensor gets entities in the *Configuration* category, on the same device.

**Only two entities are enabled by default: the firmware update entity and the *Restore previous settings* button.** Everything else below is registered but **disabled** to keep the device page quiet; enable what you need under *Settings → Devices & services → pvvx → entities (show disabled)* → entity → *Settings → Enabled*. The *Restore* button works without any of the others (it uses the settings snapshot taken before a flash). Enabling an entity does not add Bluetooth traffic: the first read only happens while some settings entity is enabled; the sensor is read in the background after setup, after a flash and then per the *How often to read the sensor's version* option (default every 3 days), never polled.

The defaults apply when an entity is first created; entities that already exist keep whatever enabled state they have. The *Beta firmware* switch only shows or sets the beta option; the option keeps its value while the switch is disabled.

| Entity | Notes |
|---|---|
| Advertising interval (number, s) | 62.5 ms steps, 0.0625 to 10 s |
| Measure interval (number) | every N advertising intervals, 2 to 25 |
| TX power (select, dBm) | a subset of the firmware's levels; an unlisted current value is shown as `raw_N` and never snapped; options are `dbm_p10_46`, `dbm_p7_79`, `dbm_p5_13`, `dbm_p3_01`, `dbm_p0_04`, `dbm_m3_03`, `dbm_m9_89`, `dbm_m15_88`, `dbm_m25_18` (p = plus, m = minus) |
| Display temperature unit (select), Comfort smiley, Screen off (switch) | **disabled by default**. LYWSD03MMC family (hardware ids 0, 3, 4, 5, 10, 14), MJWSD05MMC (9, 12), and for the unit and screen off only also MHO-C401 (1, 8), CGG1 (2, 7), CGDK2 (6), MHO-C122 (11), MJWSD06MMC (13). On e-ink boards screen off freezes the last picture |
| Smiley (select), Show battery on display (switch) | LYWSD03MMC family only (on the MJWSD05MMC the same bits mean *screen type* and *12-hour clock*, so they are not offered there) |
| Display content (select: `time`, `temperature`, `humidity`, `battery_percent`, `battery_voltage`), Two decimals on display, 12-hour clock, Show weekday (switches), Date as DD/MM (switch, MJWSD05MMC en only) | MJWSD05MMC only, disabled by default. Boards not named above (LYWSD02MMC, TS0201, ...) get no display entities; see [display-layouts.md](display-layouts.md) |
| Select option ids | The state of every select is a stable lowercase **option id**; Home Assistant shows the translated text (English and German). Use the id in automations and scripts: advertising type `atc1441`, `pvvx`, `mi`, `bthome`; temperature unit `celsius`, `fahrenheit`; smiley `off`, `happy`, `sad`, `ooo`, `frame_off`, `frame_happy`, `frame_sad`, `frame_ooo`; display content as above (the sensor can also report `external`); TX power as above. The ids do not change between releases. |
| Reflash firmware (button) | flashes the latest firmware (or the beta, if that switch is on) even when the sensor is already up to date, through exactly the same path as *Install* (shared lock, settings snapshot, progress on the update entity, post-flash check). Unavailable while a flash runs. Takes about 1.5 to 3 minutes on a good link (longer on a weak one); settings may reset (*Restore previous settings* undoes that); an interrupted flash keeps the old firmware. Never pressed automatically. |
| Reboot sensor (button) | sends the pvvx reboot command (`0x72`, "reboot on disconnect") and drops the link, so the sensor restarts in about 2 s and keeps its settings. Needs the sensor to confirm the command; unavailable while a flash runs. Also useful to get a sensor advertising again. The log says whether it was heard again within 30 s. |
| Advertising interval, Measure interval, TX power | rarely changed and easy to get wrong (a long interval makes the sensor look dead, a low TX power loses range). Enable them in the entity settings when you need them. Update, Reflash, Reboot, Refresh, Restore, Sync clock and the beta switch stay enabled. |
| Temperature offset, Humidity offset (numbers, **disabled by default**) | calibration offsets in 0.1 steps, +-12.5. They live in the sensor-coefficient command (`0x25`, not in the config block since 4.7) and are relative to the chip's factory value (taken from the firmware tables; chips not in the table get no offsets). Slope factors and everything else are written back unchanged and the sensor's reply is checked. Only change them after comparing with a reference. |
| Refresh configuration (button) | the only way the settings are re-read besides setup and after a flash; nothing is polled |
| Restore previous settings (button) | see below |

**Authentication happens in the config flow, setup is quick.** The flow only creates an entry after an authenticated read of the sensor's config block. Setting up an entry afterwards (including at every Home Assistant start) makes **no Bluetooth connection**: it only checks that a connectable scanner has heard the sensor lately (otherwise Home Assistant retries the entry with its usual back-off) and creates the entities from the stored board and firmware version. The first read of the settings then runs **in the background**: right away after a reload or a new entry, and after Home Assistant has finished starting otherwise, with the sensors spaced 4 s apart so that they do not all hit the proxies at once. Until it succeeds the settings entities show *unavailable*. A failed read is retried after 5, 15 and 60 minutes; after that the update entity's next version read (see the option) or *Refresh configuration* fills them in. It is skipped altogether while none of the entry's settings entities is enabled (the default); the update entity and *Restore previous settings* do not need it. If the sensor refuses the link in that read (a PIN was set after the entry was created, or the bond of a PIN sensor was lost) the settings stay unavailable, there is no retry loop, and the *PIN set* / *pairing lost* repair issue explains it. The sensor's clock is only synchronised in connections that are made anyway (the first read, the update entity's version read, *Refresh*, a flash). A sensor on an unverified firmware version gets the update entity, the *Beta firmware* switch and Refresh, but no settings entities and no Restore/Reset buttons.

How it is kept safe:

* The config block (command `0x55`) is read **fresh inside the write connection**, only the targeted bits/bytes are changed and the rest is sent back unchanged (advertising type, bindkey flag, PHY flags, hardware version, ...). The sensor's answer is compared with what was requested; if the firmware clamped a value you get an error naming what it stored.
* The layout was checked against the firmware sources (`src/app.h`) for **5.7, 5.8 and 5.9** only. On any other version the settings entities are not created and writes are refused. pvvx's `python-interface` is out of date for these versions (it still has the temperature/humidity offsets where the firmware now has other fields), so it was not used.
* Changes made within ~2 s of each other (e.g. from one dashboard action) share one connection. Nothing is written while a flash runs and everything uses the same one-operation-at-a-time lock as the flasher.
* Not exposed at all: `adv_flags`, connect latency, averaging, `flg3`/`event_adv_cnt`, and the temperature/humidity offsets (a separate, sensor-specific command on >= 4.7). The hardware version byte is read only (a diagnostic sensor, disabled by default).
* **Flash resets settings** on some updates. Before every flash the current settings are saved; afterwards press *Restore previous settings* if you want them back. It is never done automatically and it only writes the fields listed above that differ.

## Beta firmware

Each sensor has a **Beta firmware** switch (Configuration category, off by default, kept in the config entry options). When it is on *and* pvvx's `firmware.json` publishes a beta (`betaver` plus a `betafw` list, which is often absent) with an image for this sensor's hardware id, the update entity offers that beta (`latest_version` = `betaver`, release summary says it is a beta). Otherwise it stays on stable and the `beta_status` attribute / release summary says why ("no beta firmware for this hardware", "no beta is published", ...). A beta is only offered when it is newer than the stable release, so the switch never causes a downgrade by itself. The beta image goes through the same checks as stable ones: it must be for the same board as the stable image of this hardware, within the BLE OTA size limit, carry the Telink `KNLT` signature and match its own header length (the image CRC is not checked here, the sensor does that, see below). `betafw` entries may be full URLs (like pvvx's web flasher), but only on `raw.githubusercontent.com/pvvx/...` or `github.com/pvvx/...`; anything else is ignored.

## Options

Each sensor has options (Settings → Devices & services → pvvx → Configure):
* **Minimum battery voltage for a flash** (default 2.5 V),
* **Set the clock automatically** (default on),
* **Switch BTHome encryption back on after a flash** (default on),
* **How often to read the sensor's version** over Bluetooth: every day, every 3 days (default), every 7 days or never (then only on demand with the Refresh button, after a flash and in the first read after a start). The firmware list and the update offers are refreshed separately every 12 hours over the internet, without Bluetooth, so offers do not depend on this setting. It applies as soon as you save it.

## Update all (queue)

Firmware operations (Install, Reflash, `flash_latest`, `flash_file`) run one at a time. A second one is **queued** instead of refused: the update entity shows `in_progress` without a percentage and a `queued_position` attribute, first come first served, for up to 2 h. Settings → Updates → *Update all* therefore flashes your sensors one after another. Unloading an entry cancels its queued job.

**Stored firmware list.** The last good `firmware.json` (and the release notes) is also kept in Home Assistant's storage (`pvvx_ota.firmware_catalog`). After a restart it is the first catalog, marked stale, and a live fetch is still tried with the normal retry and backoff. While GitHub is unreachable the stored copy is used for offering updates, the board check and `flash_file`; a download of an image that is not cached still fails. Diagnostics show the catalog's `source` (`live` or `stored`), when it was fetched and its age, and the log says once at INFO when the stored copy is in use.

**Flashing a file without the board check.** `pvvx_ota.flash_file` normally derives the board from the firmware list (live or stored) and compares it with the file name, and it refuses when no list is available. With `skip_board_check: true` only that check is skipped (no list is needed, so this also works without internet access); the signature, size and header checks, the battery check (`force` is a separate option for that), the slow-link stop and everything else still apply. Every such flash logs a WARNING with the file and the sensor. A wrong image can stop a sensor from working: use it only for a file you know is right. Install, Reflash and *Update all* never skip the check, and there is no entity or switch for it.

## Example automations

See [examples.md](examples.md).

## Clock

pvvx sensors keep their own clock (display on some models, timestamps for the history). The *Sync clock* button sets it to your local time with command `0x23`, exactly the payload TelinkMiFlasher's "Set Time" sends (local wall-clock time as epoch seconds; verified in pvvx `cmd_parser.c` and the flasher). The integration also checks the clock inside connections it makes anyway (setup/refresh read, the version read, the read before a flash, the reboot button) and sets it only if it is more than 5 s off, so there are no extra connections. This can be switched off in the integration options. Failures never affect the connection.

## Release notes

The update entity shows what changed: `release_url` points at pvvx's "Firmware version history" table and `release_summary` / the release notes dialog are built from that table in pvvx's README for the versions between the installed and the offered one (cached like the firmware list, never required: if the README cannot be read you get a link instead). Example: 5.7 → 5.9 shows that 5.8 changed how settings are saved in flash and that a reconfiguration is required.

## Battery check

Before a flash the integration looks at the battery voltage of the linked BTHome device (its voltage sensor, no extra Bluetooth traffic) and refuses below 2.5 V with a clear message: a flash that dies half way because the battery is empty is not worth it. `flash_latest` and `flash_file` accept `force: true` to flash anyway. If no voltage is known it warns in the log and continues.

## After a flash

A flash can clear settings. The integration therefore saves them first and, once the post-flash version check succeeded:
* **A flash across firmware 5.7/5.8 (either direction)** erases everything the sensor stored, including its encryption key. Before such a flash the integration reads the key (only if encryption is on) and keeps it in memory; after the version check it writes the key back, switches encryption on and writes the saved settings back, without any action from you. If that does not work (Home Assistant restarted meanwhile, the sensor did not accept the key) encryption stays off and the repair issue *the flash replaced the encryption key* explains the fix: `pvvx_ota.set_encryption` with your old key (so BTHome keeps working), or without a key to get a new one that you enter in the BTHome entry. `keep_key` is refused while that issue is open. The **PIN and the temperature/humidity calibration coefficients** are erased as well and are not carried over: set them again (a PIN sensor: see [pin-sensors.md](pin-sensors.md)).
* **BTHome encryption** (flash on the same side of 5.8) is switched back on automatically if it was on before and the sensor still holds its key (the key itself is never read out, written or logged: only the flag is set again). If that fails, a repair issue explains how to fix it with `pvvx_ota.set_encryption` and your existing key.
* **All other settings** are never re-applied automatically. If they differ from before the flash, a repair issue points at the *Restore previous settings* button; it clears when the settings match again.
* No other connection is opened to the sensor for 30 s after a flash (it is rebooting, which takes a few seconds) and the first version check runs after 30 s.

## Diagnostics and languages

* *Download diagnostics* on a sensor's entry gives the entry data/options, the decoded config block (it holds no key), the firmware catalog summary, the timings and result of the last flash, the proxy the last connection really used and which device the entities sit on. Bindkey/PIN-like fields are redacted. The bindkey is never stored; the PIN of a PIN-bonded sensor is stored in its config entry (see [pin-sensors.md](pin-sensors.md)) and redacted here.
* The user interface is available in English and German (`translations/de.json`). The selectable *values* of the selects (smiley, advertising type) stay English so that automations that use them keep working.

## Dangerous settings

These can make a sensor disappear from Home Assistant or lock out this integration. Every one is opt-in: the entities are **disabled by default** (enable them in the entity settings) and the actions are admin-only. All of them follow the same rules as the other settings (verified firmware only, read back, one operation at a time, never during a flash).

| What | How | Consequence |
|---|---|---|
| Advertising type (select) | disabled entity | Anything but **BTHome** stops HA's `bthome` integration from receiving the sensor's data. A repair issue is raised until a read shows BTHome again (also when it was changed elsewhere). |
| Long range PHY / BT5 PHY (switches) | disabled entities | ESP32 proxies may not receive coded-PHY adverts: the sensor can vanish from HA until it is power cycled (long range resets on power-off). |
| Encryption + bindkey | action `pvvx_ota.set_encryption` (`enable`, optional `key`, optional `keep_key`) | Stores a key (generated if you give none) and switches encrypted adverts on, or switches them off. The **key is returned only in the action's response** (Developer tools > Actions shows it) and is never put in an entity, attribute or log. Home Assistant's BTHome entry for the sensor then needs that key; this integration does not touch it. With `keep_key: true` (and no `key`) only the flag is switched on and the key the sensor still holds stays as it is (nothing is returned, the BTHome entry keeps its key); it fails if the sensor holds no key. A repair issue reminds you and clears by itself once Home Assistant receives a reading from the sensor through BTHome again. |
| PIN | action `pvvx_ota.set_pin` (`pin` 0..999999, `confirm: true`) | **Refuses unless `confirm: true`.** A PIN needs the proxy bond described in [pin-sensors.md](pin-sensors.md); setting or changing the PIN erases the sensor's bonds, so pair again afterwards (and with a bond-less PIN the integration can no longer connect). TelinkMiFlasher can remove the PIN. Write only: the PIN is never read back, and the action neither stores nor logs it (see the event-bus note in [pin-sensors.md](pin-sensors.md)). |
| Reset to defaults (button) | disabled entity | Sets the config block to factory values (advertising type and encryption flag too), so HA's BTHome link may break. The previous settings are saved first and the *Restore previous settings* button offers them again (the encryption flag too, if it was on and the sensor still holds its key; never switched off). A repair issue is raised as well; its *Submit* does the same restore and the issue clears once a read shows the saved settings again. |

Notes: values passed to actions can be visible in HA's event bus and automation traces if you call them from an automation; use Developer tools > Actions for the key and the PIN. Home Assistant's own debug logging for `bluetooth`/`habluetooth`/`bleak_esphome` may record raw GATT traffic; do not enable it while setting a key or PIN. This integration itself never logs them.

## Safety
* Only one Bluetooth operation (read or flash) at a time across the whole integration.
* Refuses to flash if the hardware id is unknown, the catalog entry is `?`, the reply isn't pvvx's, the image lacks the Telink `KNLT` signature, is shorter than its own header size field (truncated), exceeds the 128 KiB BLE-OTA limit, or the local file name does not match the board.
* Per-write (10 s) and overall (20 min) timeouts; always disconnects, also on error.
* The integration checks the image before it connects (signature, size limit, header length, board by file name); it does **not** compute the image CRC itself. After the last block the device verifies the image CRC and refuses a bad image (it then keeps the old firmware). Only a real disconnect after the end marker counts as "the sensor is rebooting"; a stalled link (reads time out and the link is still up after 10 s, the supervision timeout we ask for plus a margin) ends the flash as unverified with a failure issue, and the version check afterwards clears it when it reads the new version. The entity re-reads the version 30 s later (up to 8 tries, 30 s apart); if that fails the installed version is left unchanged (it may still be on the old image).

## Settings after a flash
Observed once (5.7 -> 5.9): the sensor came back with BTHome advert encryption disabled and HA's BTHome integration asked to confirm the change. This integration only writes the image and never touches the sensor's settings or key, but the update or the new firmware evidently changed them. Before flashing, note the sensor's settings (and bindkey if encrypted) and check them in TelinkMiFlasher afterwards. The exposed settings (above) are saved automatically before a flash and can be restored with a button; advert encryption/bindkey is not among them and must still be checked by hand.

## Removal

* **Remove one sensor:** *Settings → Devices & services → pvvx Thermometer OTA →* the sensor's entry *→ ⋮ → Delete*. The integration's entities and any of its repair issues for that sensor disappear. **Nothing is changed on the sensor**: its firmware and settings stay as they are, and it keeps advertising. The sensor's *BTHome* entry, device, name, area and entities are never touched; the update and settings entities simply leave that device page. A sensor that was ignored because it has a PIN is removed the same way (*Ignored* section, *Delete*), after which it is offered again.
* **Remove the integration:** delete every entry as above, then (HACS) *Marketplace → pvvx Thermometer OTA → Remove*, or delete `custom_components/pvvx_ota` by hand, and restart Home Assistant. Any automations that used the `pvvx_ota.*` actions need to be removed first.

## Limitations
* Data blocks are 16 bytes each. They are sent as write-without-response with a status read every 8 blocks (`ota.DATA_RESPONSE`, `ota.STATUS_EVERY`); lost blocks are caught by the device's index/CRC checks and abort the flash. Through an ESPHome proxy a flash takes a few minutes on a good link (see *Faster link* below), versus an estimated ~35 min with a write-with-response per block. The proxy holds one of its few connection slots the whole time. Keep the sensor close.
* pvvx sensors are connectable only part of the time; "not connectable" errors mean retry in a minute.
* Discovery is only a hint and never connects: matchers are `ATC_*` names, service data 0x181A (pvvx custom/ATC format) and 0xFCD2 (BTHome v2, shared with Shelly BLU etc.). An advert is offered only if the name is `<MODEL>_<6 hex>`, it is the 0x181A format, or the MAC starts with `A4:C1:38` (passive proxies never see the name; encrypted BTHome hides the payload). The **confirm step then connects once** and sends pvvx's config command 0x55: no answer or no pvvx characteristic aborts with "not a pvvx device"; otherwise the board id and version are stored. The manual picker lists every BTHome/0x181A device.
* pvvx's BTHome beacon carries no firmware-version object (checked `bthome_beacon.[ch]`), and its 0x180A strings are stock-compatible, so the installed version always comes from the GATT read above.
* **Faster link during a flash (experimental):** right before the OTA start (and once after it) the integration asks the proxy for a 7.5 ms connection interval, latency 0 (`set_connection_params(6, 6, 0, 600)`, ESPHome 2026.10+ proxies). It is asked again about 2 s after the connect (the sensor asks for 20 ms / latency 49 about 1 s after connecting; that delayed request made a flash about 8x faster). Every failure or missing support is logged at DEBUG and ignored, the flash continues unchanged. DEBUG timing logs (enable debug logging for `custom_components.pvvx_ota` when reporting a problem) (time since connect, per-window ms/block for the first 15 s, which proxy/RSSI) help diagnose the link. See [design-notes.md](design-notes.md).
* **Connecting is bounded** (25 s for all retries and proxies): a sensor that does not accept a connection is reported as "could not connect" instead of the flow hanging. pvvx sensors are connectable only part of the time.
* **PIN-protected sensors need a proxy with the pairing package** (see [pin-sensors.md](pin-sensors.md)). Without one, a sensor that answers "insufficient encryption/authentication" on the first attempt is refused in the setup flow ("This sensor has a PIN set") and remembered as an *ignored* entry (discovery stops offering it); an existing entry whose sensor later gets a PIN raises the *PIN set* repair issue. A BTHome *bindkey* (advert encryption) is unrelated.
* **Devices:** since HA 2026.10 a device belongs to exactly one config entry, so the update entity is *attached to the existing BTHome device* (found by its Bluetooth MAC) instead of creating a second device; it shows on that device's page and the BTHome device, its name, area and entities are never touched. If there is no BTHome device (yet) the integration creates its own device and re-links the entity automatically when a BTHome device appears (or falls back when it is removed). Devices left over from earlier versions are removed. The entity's `unique_id` never changes, so entity IDs and history survive.
* Installed version needs a GATT connection, so it is read at startup, after a flash and every 3 days by default (an option) (or via `homeassistant.update_entity`); the last value is kept in the config entry.
* No "specific version" selection: pvvx GitHub releases carry no binaries, and `firmware.json` only lists the current build.
* `firmware.json` stores the version byte in decimal (89 = 0x59 = **V5.9**, file `_v59`); the beta (`betaver`) is shown as an attribute only.
