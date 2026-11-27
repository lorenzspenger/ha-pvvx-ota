# Known limitations

Back to the [README](../README.md). These are limits of the integration or of the sensors, not bugs.

* **pvvx custom firmware only.** The first flash from stock firmware needs the web flasher.
* **Settings only for verified firmware** (4.2 to 5.5, 5.7, 5.8, 5.9). Other versions get the update entity only.
* **No choice of version.** pvvx publishes no binaries per release; the update offers the build listed in `firmware.json` (and optionally its beta).
* **Sensors are connectable only part of the time.** "Not connectable" means try again in a minute.
* **A flash needs a decent link.** Below about 15 blocks/s the flash is stopped before the end marker and the old firmware stays. Use a proxy closer than about -75 dBm.
* **One operation at a time** for all sensors: a flash blocks configuration changes and other flashes until it is done.
* **PIN-protected sensors are experimental**: they need a proxy with the pairing package, and some sensors refuse even after a correct pairing ([pin-sensors.md](pin-sensors.md)).
* **The installed version is read over Bluetooth**, so it can be a few days old (see [data-updates.md](data-updates.md)).
* **A flash can change settings** (including BTHome encryption). Only the encryption flag is restored automatically; other settings have a *Restore previous settings* button, which also switches the encryption flag back on when it was on and the key is still on the sensor.
* **The integration shares the BTHome device.** The update and settings entities appear on the sensor's BTHome device, so the integration is not listed as its own device owner.
* **Display values of selects stay English** so that automations keep working.
