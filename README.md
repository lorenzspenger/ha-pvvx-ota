# pvvx Thermometer OTA

Home Assistant custom integration that updates and configures [pvvx ATC_MiThermometer](https://github.com/pvvx/ATC_MiThermometer)
custom firmware over Home Assistant's own Bluetooth stack, so it works through ESPHome Bluetooth proxies without extra setup. Sensors with
a PIN are supported through a small ESPHome package. **Public beta (0.23.0-beta.1).**

> **Risk notice.** Flashing over Bluetooth can fail (weak link, empty battery, bugs). The integration checks the image, refuses a low
> battery and stops a too-slow flash before the final step, so the old firmware normally keeps running, but a flash uses battery and an
> update may reset settings. **Use at your own risk, without warranty.** Try one sensor first and keep the
> [web flasher](https://pvvx.github.io/ATC_MiThermometer/TelinkMiFlasher.html) at hand.

## Requirements

* Home Assistant 2026.10.0 or newer.
* ESPHome Bluetooth proxies with active connections (ESPHome 2026.10+ recommended), or a local adapter that can connect.
* pvvx custom firmware already on the sensor (the first flash from stock firmware needs the web flasher).

## Install

1. HACS → three-dot menu → *Custom repositories* → add `https://github.com/lorenzspenger/ha-pvvx-ota`, type *Integration*.
2. Open the repository in HACS, enable **Show beta versions**, *Download*, restart Home Assistant.
   (Manual: copy `custom_components/pvvx_ota` into `<config>/custom_components/`.)

## Setup

*Settings → Devices & services → Add integration → pvvx Thermometer OTA*, or accept a discovered sensor. Each sensor gets an update entity;
most settings entities are disabled by default.

For sensors with a PIN, add this to **one** proxy's ESPHome YAML; Home Assistant then asks for the PIN when you add the sensor:

```yaml
packages:
  pvvx_pin_pairing: github://lorenzspenger/ha-pvvx-ota/esphome/pvvx_pin_pairing.yaml@main

esp32_ble:
  max_connections: 4   # the proxy's slots + other ble_clients + 1
```

## Documentation

* [Usage](docs/usage.md): entities, options, actions, flashing details, supported boards and firmware
* [PIN-protected sensors](docs/pin-sensors.md)
* [Troubleshooting](docs/troubleshooting.md): diagnostics, logs, slow links, known issues
* [Design notes](docs/design-notes.md), [display layouts](docs/display-layouts.md), [firmware versions](docs/version-layouts.md)

Bug reports: [issues](https://github.com/lorenzspenger/ha-pvvx-ota/issues) (please attach the diagnostics download).

## License

[MIT](LICENSE)
