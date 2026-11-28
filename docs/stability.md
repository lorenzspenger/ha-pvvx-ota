# What stays stable from 1.0

From version 1.0 this integration follows semantic versioning for the interfaces below. Changing a stable one is a breaking change and needs a new major version. Experimental ones can change in a minor release; the changelog says so. Everything else can change at any time. Back to the [README](../README.md) or [usage](usage.md).

## Stable

Names and ids below are exactly what the code uses.

### Integration and entities

* The integration domain: `pvvx_ota`.
* The unique id of every entity: `{MAC}_{key}`, with the Bluetooth address in upper case with colons (`AA:BB:CC:DD:EE:FF_adv_interval`). The entity keys per platform:
  * update: `firmware`
  * number: `adv_interval`, `measure_interval`, `temp_offset`, `humi_offset`
  * select: `rf_tx_power`, `temp_unit_display`, `smiley`, `adv_type`, `screen_type`
  * switch: `show_battery`, `comfort_smiley`, `screen_off`, `longrange`, `bt5phy`, `beta_firmware`, `x100`, `time_am_pm`, `date_ddmm`, `show_weekday`
  * button: `refresh_config`, `reflash_firmware`, `restore_config`, `reset_defaults`, `reboot_sensor`, `sync_clock`
  * sensor: `hw_ver`
* The option ids (state values) of each select:
  * `rf_tx_power`: `dbm_m25_18`, `dbm_m15_88`, `dbm_m9_89`, `dbm_m3_03`, `dbm_p0_04`, `dbm_p3_01`, `dbm_p5_13`, `dbm_p7_79`, `dbm_p10_46`, and `raw_<N>` for a value the sensor reports that the integration does not list (never selectable)
  * `temp_unit_display`: `celsius`, `fahrenheit`
  * `smiley`: `off`, `happy`, `sad`, `ooo`, `frame_off`, `frame_happy`, `frame_sad`, `frame_ooo`
  * `adv_type`: `atc1441`, `pvvx`, `mi`, `bthome`
  * `screen_type`: `time`, `temperature`, `humidity`, `battery_percent`, `battery_voltage`, and `raw_<N>` for a reported value that is not listed; `external` is only ever reported, never selectable
* The extra attributes of the update entity: `hardware_id`, `board`, `beta_version`, `beta_firmware_option`, `beta_status`, `last_error`, `expected_proxy`, `expected_proxy_rssi`, `queued_position`. An attribute that has no value is left out (all but `hardware_id`).

### Config entry options

| Option id | Values | Default |
|---|---|---|
| `min_battery_v` | number, 1.8 to 3.2 V in steps of 0.05 | 2.5 |
| `auto_clock_sync` | true / false | true |
| `auto_restore_encryption` | true / false | true |
| `device_poll_interval` | `1d`, `3d`, `7d`, `never` | `3d` |
| `beta` | true / false (the *Beta firmware* switch writes it) | false |

### Actions

All are admin only and target update entities of this integration.

| Action | Fields | Response |
|---|---|---|
| `pvvx_ota.flash_latest` | `force` (boolean, default false), `version` (text, optional) | none |
| `pvvx_ota.flash_file` | `path` (text, required), `force` (boolean, default false), `skip_board_check` (boolean, default false) | none |
| `pvvx_ota.set_encryption` | `enable` (boolean, required), `key` (32 hex characters, optional), `keep_key` (boolean, default false) | `encryption` (boolean) and `note` (text); `bindkey` (32 hex characters) when a key was stored |
| `pvvx_ota.set_pin` (experimental) | `pin` (0 to 999999, 0 = off, required), `confirm` (boolean, must be true) | `pin_set` (boolean); `paired` (boolean) when the entry is bonded to a proxy and a PIN was set |

### The ESPHome pairing package

The file `esphome/pvvx_pin_pairing.yaml` is a contract between the integration and a proxy that runs it. Stable here means that a proxy with the package of one 1.x release keeps working with every later 1.x integration, or the repair issue *pairing package out of date* says what to do.

* Actions: `pair_pvvx(mac, pin)`, `forget_pvvx(mac)` and `pvvx_clear_pin(mac, pin)`. `mac` is text (`AA:BB:CC:DD:EE:FF`), `pin` an integer.
* Entities, found by the names the package gives them: binary sensor *pvvx pairer busy*, text sensors *pvvx pair result* and *pvvx package version* (the version is a whole number).
* The texts of *pvvx pair result*: `pairing` (while a run is going), `passkey answered`, `no passkey asked`, `no connection`, `timeout`, `pin clear sent`, `clear not sent`.
* The package version, currently **2**. Any change to the actions, their parameters, the names of the entities or the result texts raises it, in the package and in the integration (`PAIRING_PACKAGE_VERSION`) together.

The PIN feature as a whole is experimental (next section); the package contract is frozen so that a proxy's YAML does not break, not because PIN support is promised to work on every sensor.

## Experimental

These can change in a minor release.

* Everything about PIN sensors: the PIN step of the setup flow, `pvvx_ota.set_pin`, the pairing package and its `pvvx_clear_pin` action, and the config entry data that belongs to them. See [pin-sensors.md](pin-sensors.md).
* Installing a specific firmware version (the `version` field of `flash_latest` and the version choice of the update entity).
* The details of how keys and settings are carried over a flash across the 5.7/5.8 boundary (EEPROM layout change).

## Not stable

These can change at any time and need no changelog entry beyond a mention.

* The layout of the diagnostics file, including the clock drift measurement (`clock_drift`: elapsed time, offset, ppm, old and new tick step, result).
* Log messages.
* The ids and texts of repair issues, descriptions, errors and all translations.
* Timing values: timeouts, quiet periods, retry waits, poll intervals that are not an option.
* The firmware version table and which versions are offered or flagged.
* Module, class and function names inside `custom_components/pvvx_ota`, the config entry data keys (apart from what is listed above as an option), the format of the stored files.
