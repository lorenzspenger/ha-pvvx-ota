# pvvx Thermometer OTA

Home Assistant custom integration that updates and configures [pvvx ATC_MiThermometer](https://github.com/pvvx/ATC_MiThermometer)
custom firmware over Home Assistant's own Bluetooth stack, so it works through ESPHome Bluetooth proxies without extra setup. Sensors with
a PIN are supported through a small ESPHome package. **Public beta (0.23.0-beta.1).**

> **Risk notice.** Flashing over Bluetooth can fail (weak link, empty battery, bugs). The integration checks the image, refuses a low
> battery and stops a too-slow flash before the final step, so the old firmware normally keeps running, but a flash uses battery and an
> update may reset settings. **Use at your own risk, without warranty.** Try one sensor first and keep the
> [web flasher](https://pvvx.github.io/ATC_MiThermometer/TelinkMiFlasher.html) at hand.

## What you get

* An **update entity** per sensor: installed version read over Bluetooth, latest from pvvx's `firmware.json`, install with progress,
  release notes, optional beta firmware. *Update all* flashes your sensors one after another.
* **Safety checks**: image checked for the sensor's own board, battery check, settings saved before a flash (restore button afterwards),
  slow-link abort before the end marker, repair issues for failed flashes.
* **Sensor settings** as entities (intervals, TX power, display options, calibration offsets, clock sync, reboot). Most are disabled
  by default; enable what you need.
* **Actions**: `pvvx_ota.flash_latest`, `pvvx_ota.flash_file` (local `.bin`), `pvvx_ota.set_encryption`, `pvvx_ota.set_pin` (admin only).
* **PIN-protected sensors** through an ESPHome proxy package (below).
* Diagnostics download, English and German UI.

## Requirements

* Home Assistant **2026.10.0** or newer.
* ESPHome Bluetooth proxies with **active connections** (ESPHome 2026.10+ recommended for fast flashes), or a local adapter that can connect.
  Keep the sensor close to its proxy (better than about -75 dBm).
* pvvx custom firmware **already on the sensor** (the first flash from stock firmware needs the web flasher).
* Firmware 4.2 to 5.5, 5.7, 5.8 and 5.9 get the full settings; other versions only the update entity. Boards: see [usage](docs/usage.md).

## Install

1. HACS → three-dot menu → *Custom repositories* → add `https://github.com/lorenzspenger/ha-pvvx-ota`, type *Integration*.
2. Open the repository in HACS, enable **Show beta versions**, *Download*, restart Home Assistant.
   (Manual: copy `custom_components/pvvx_ota` into `<config>/custom_components/`.)

## Setup

*Settings → Devices & services → Add integration → pvvx Thermometer OTA*, or accept a discovered sensor (it advertises as `ATC_xxxxxx`).
The entry confirms with one Bluetooth connection. The update entity appears on the sensor's existing BTHome device.

### Sensors with a PIN

Home Assistant cannot type a PIN itself, so one ESPHome proxy pairs the sensor and keeps the bond. Add the package to **one** proxy
(the one that hears the sensor best); the add-sensor flow then asks for the PIN and pairs through it:

```yaml
packages:
  pvvx_pin_pairing: github://lorenzspenger/ha-pvvx-ota/esphome/pvvx_pin_pairing.yaml@main   # or pin a tag, e.g. @v0.23.0-beta.1

esp32_ble:
  max_connections: 4   # the proxy's connection slots + other ble_clients + 1 for the pairer
```

Good to know: the package needs ESP-IDF and one spare connection slot; the PIN is held in RAM only and stored (redacted) in the config
entry for automatic re-pairing; a PIN sensor works through that proxy only. Alternatively download the file and use
`packages: !include pvvx_pin_pairing.yaml`, or pair by hand with your own `ble_client`. Details, limits and the fix for a sensor that
still refuses: [PIN-protected sensors](docs/pin-sensors.md).

## Known issues (beta)

* A sensor can refuse after a correct pairing: update it and reset its PIN with TelinkMiFlasher ([checklist](docs/pin-sensors.md)).
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
