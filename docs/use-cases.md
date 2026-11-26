# Use cases

Back to the [README](../README.md).

* **Keep the firmware of your pvvx thermometers up to date from Home Assistant**, without a phone and the web flasher: one update entity per sensor, *Update all* flashes them one after another, release notes show what changed.
* **Flash through your ESPHome Bluetooth proxies**: the sensor does not need to be near the Home Assistant host; any proxy that hears it and has a free connection slot works.
* **Change sensor settings from a dashboard**: advertising and measure interval, TX power, display options per board, calibration offsets, clock sync and reboot, with the settings saved before a flash and a button to restore them.
* **Know when something went wrong**: repair issues for a failed or unconfirmed flash, lost pairing, encryption that was not switched back on, and settings that differ after a flash.
* **Use sensors with a PIN** (experimental): pair them once through a proxy that has the pairing package ([pin-sensors.md](pin-sensors.md)).
* **Flash your own build**: `flash_file` with a local `.bin` for a board you compiled or a specific version.
