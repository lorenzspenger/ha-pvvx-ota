# Supported devices

Back to the [README](../README.md).

The integration works with **Telink-based thermometers that already run pvvx custom firmware** ([ATC_MiThermometer](https://github.com/pvvx/ATC_MiThermometer)). The first flash from stock firmware needs pvvx's web flasher. Stock Xiaomi firmware, other BTHome devices and sensors with other chips are not supported.

## Firmware versions

* **Full support (update entity and settings)**: 4.2 to 5.9 (every tag, 5.6 only as beta commits). The settings layout of these versions was compared with pvvx's sources ([version-layouts.md](version-layouts.md)).
* **Update entity only**: 4.1 and older and anything newer than 5.9. No settings entities, and the configuration block is never written.

## Boards

The board is the one pvvx's [`firmware.json`](https://github.com/pvvx/ATC_MiThermometer/blob/master/firmware.json) lists for the sensor's hardware id, so every board it lists can be updated. Settings entities depend on the board:

* **LYWSD03MMC family** (hardware ids 0, 3, 4, 5, 10, 14) and **MJWSD05MMC** (9, 12): the full display options.
* **MHO-C401** (1, 8), **CGG1** (2, 7), **CGDK2** (6), **MHO-C122** (11), **MJWSD06MMC** (13): temperature unit and screen off.
* Other boards (for example LYWSD02MMC or TS0201): no display options.

Which bits mean what on each board: [display-layouts.md](display-layouts.md). Most testing was done on LYWSD03MMC and MJWSD05MMC.

## Bluetooth

* An ESPHome Bluetooth proxy with active connections (ESPHome 2026.10 or newer is recommended), or a local adapter that can connect.
* Home Assistant 2026.10.0 or newer.
