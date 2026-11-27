# Supported functions

Back to the [README](../README.md). Every sensor is one config entry. Its entities sit on the sensor's BTHome device (or on a device of their own when there is no BTHome device).

## Entities

| Platform | Entity | Enabled by default |
|---|---|---|
| update | Firmware: installed and latest version, install with progress, release notes, optional beta | yes |
| button | Restore previous settings | yes |
| button | Refresh configuration, Sync clock, Reboot sensor, Reflash firmware, Reset settings to defaults | no |
| number | Advertising interval, Measure interval, Temperature offset, Humidity offset | no |
| select | TX power, Display temperature unit, Smiley, Display content, Advertising type | no |
| switch | Beta firmware, Show battery, Comfort smiley, Screen off, Two decimals, 12-hour clock, Date as DD/MM, Show weekday, Long range PHY, BT5 PHY | no |
| sensor | Hardware version id (diagnostic) | no |

Which of them exist depends on the sensor's firmware and board ([supported-devices.md](supported-devices.md)); all except the update entity are in the *Configuration* category. Details and notes per entity: [usage.md](usage.md).

## Actions

* `pvvx_ota.flash_latest`: flash the latest release even if the sensor is up to date.
* `pvvx_ota.flash_file`: flash a local `.bin` (options `force` for the battery check and `skip_board_check`).
* `pvvx_ota.set_encryption`: switch BTHome advert encryption on or off (returns the key it set).
* `pvvx_ota.set_pin`: set or clear a PIN (needs `confirm: true`).

All are admin only and target the sensor's update entity. Install, Reflash and *Update all* use the same path as `flash_latest`.

## Options

Minimum battery voltage for a flash, automatic clock sync, switching BTHome encryption back on after a flash, and how often the sensor's version is read. See [usage.md](usage.md).

## Repair issues and diagnostics

Failed or unconfirmed flash, a PIN set, pairing lost, encryption not restored or changed, advertising type changed, settings reset or different after a flash. *Download diagnostics* on a sensor's entry (Bluetooth addresses and proxy names redacted) shows the decoded configuration block, the firmware list summary and the last flash.
