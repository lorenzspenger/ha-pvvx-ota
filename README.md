# pvvx Thermometer OTA

Home Assistant custom integration that updates and configures [pvvx ATC_MiThermometer](https://github.com/pvvx/ATC_MiThermometer) custom firmware over Home Assistant's own Bluetooth stack, so it works through ESPHome Bluetooth proxies without extra setup. **Version 0.24.0-beta.1.**

> **Supported: pvvx sensors without a PIN.** Sensors with a PIN only work through an *experimental, not stable* workaround (an ESPHome package) that can fail on some sensors. For reliable use, remove the PIN with TelinkMiFlasher. See [PIN-protected sensors](docs/pin-sensors.md).

> **Risk notice.** Flashing over Bluetooth can fail (weak link, empty battery, bugs). The integration checks the image, refuses a low battery and stops a too-slow flash before the final step, so the old firmware normally keeps running, but a flash uses battery and an update may reset settings. **Use at your own risk, without warranty.** Try one sensor first and keep the [web flasher](https://pvvx.github.io/ATC_MiThermometer/TelinkMiFlasher.html) at hand.

## What you get

* An **update entity** per sensor: installed version read over Bluetooth, latest from pvvx's `firmware.json`, install with progress, release notes, optional beta firmware. *Update all* flashes your sensors one after another.
* **Safety checks**: image checked for the sensor's own board, battery check, settings saved before a flash (restore button afterwards), slow-link abort before the end marker, repair issues for failed flashes.
* **Sensor settings** as entities (intervals, TX power, display options, calibration offsets, clock sync, reboot). Most are disabled by default; enable what you need.
* **Actions**: `pvvx_ota.flash_latest`, `pvvx_ota.flash_file` (local `.bin`), `pvvx_ota.set_encryption`, `pvvx_ota.set_pin` (admin only).
* PIN-protected sensors: experimental workaround only, see [docs/pin-sensors.md](docs/pin-sensors.md).
* Diagnostics download, English and German UI.

## Requirements

* Home Assistant **2026.10.0** or newer.
* ESPHome Bluetooth proxies with **active connections** (ESPHome 2026.10+ recommended for fast flashes), or a local adapter that can connect. Keep the sensor close to its proxy (better than about -75 dBm).
* pvvx custom firmware **already on the sensor** (the first flash from stock firmware needs the web flasher).
* Firmware 4.2 to 5.5, 5.7, 5.8 and 5.9 get the full settings; other versions only the update entity. Boards: see [usage](docs/usage.md).

## Install

1. HACS → three-dot menu → *Custom repositories* → add `https://github.com/lorenzspenger/ha-pvvx-ota`, type *Integration*.
2. Open the repository in HACS, *Download*, restart Home Assistant. (Manual: copy `custom_components/pvvx_ota` into `<config>/custom_components/`.)

## Setup

*Settings → Devices & services → Add integration → pvvx Thermometer OTA*, or accept a discovered sensor (it advertises as `ATC_xxxxxx`). The entry confirms with one Bluetooth connection. The update entity appears on the sensor's existing BTHome device.

## Known issues

* PIN sensors are experimental: a sensor can refuse even after a correct pairing ([details and fix](docs/pin-sensors.md)).
* A slow or weak link aborts the flash on purpose; the old firmware keeps running. Move the proxy closer and retry.
* An update can switch off BTHome encryption or reset settings; the integration restores what it can and raises repair issues.

## Documentation

* [Usage](docs/usage.md): entities, options, actions, flashing details, supported boards and firmware
* [PIN-protected sensors](docs/pin-sensors.md)
* [Troubleshooting](docs/troubleshooting.md): diagnostics, logs, slow links, known issues
* [Design notes](docs/design-notes.md), [display layouts](docs/display-layouts.md), [firmware versions](docs/version-layouts.md)

Bug reports: [issues](https://github.com/lorenzspenger/ha-pvvx-ota/issues) (please attach the diagnostics download).

## License

[MIT](LICENSE)
