# Troubleshooting

Back to the [README](../README.md).

## Reporting problems

Open an [issue](https://github.com/lorenzspenger/ha-pvvx-ota/issues) and attach the sensor entry's *Download diagnostics* (*Settings → Devices & services → pvvx Thermometer OTA →* the sensor *→ ⋮*) and the log lines containing `pvvx_ota`. Diagnostics redact PINs and keys; look through the log before you paste it anyway.

## Slow links

A flash is judged on its first seconds: if fewer than about 15 blocks/s get through (a normal link does 60 and more), the fast connection parameters are requested once more and measured again; if it is still that slow the flash is stopped **before the end marker**, so the sensor keeps its old firmware, and the failure says the rate, the proxy and the signal ("link too slow ... try again later or move the sensor's pairing to a closer proxy"; the usual failed-flash repair issue appears). Before a flash a warning is logged when the proxy it will use hears the sensor below -75 dBm (never a refusal), and the update entity's attributes `expected_proxy` and `expected_proxy_rssi` show which proxy that is.

## Common problems

* **"This sensor has a PIN set and no proxy has a bond with it" / the sensor is not offered any more.** The setup flow adds it as an *ignored* entry so discovery stops asking. Pair it from a proxy ([pin-sensors.md](pin-sensors.md)) or remove the PIN in TelinkMiFlasher, then delete the ignored entry (*Settings → Devices & services → Ignored*) and the sensor is offered again.
* **"Needs reconfiguration" on a PIN sensor / "pairing lost".** The bond with its proxy is dead and no PIN is stored (or it did not help). Click *Reconfigure* on the notice and enter the PIN; a proxy that offers `pair_pvvx` pairs again. See *Pairing a PIN sensor*.
* **The proxy paired with the PIN, but the sensor still refuses ("could not be paired with its PIN").** This is a problem on the sensor, usually an old pairing entry or old firmware, and trying other proxies does not help (each one adds a bond to the sensor's small table). Fix it once with [TelinkMiFlasher](https://pvvx.github.io/ATC_MiThermometer/TelinkMiFlasher.html) near the sensor:
  1) connect and update the firmware to the latest version, 2) set the PIN to 0 and save, then set your PIN again (this clears the sensor's pairing memory), 3) add the sensor here again.
* **"Not reachable via the paired proxy".** The proxy that holds the bond does not hear the sensor right now. Nothing is lost: wait, or bring that proxy closer.
* **"Could not connect (not a PIN problem)" / the sensor is unreachable.** pvvx sensors are connectable only part of the time and weak links fail. Check that a Bluetooth proxy hears it well (RSSI better than about -85 dBm), move a proxy closer and retry.
* **Encryption was switched off by a flash.** A flash clears the advert-encryption flag but not the key. The integration switches the flag back on by itself (option *Restore encryption automatically*); if that fails a repair issue says so and the `pvvx_ota.set_encryption` action with your existing key fixes it.
* **The flash is slow or fails.** Use a proxy close to the sensor and with a free connection slot. A failed flash leaves the old firmware running, raises the repair issue *firmware update failed* (it clears after the next successful flash) and records the error in the diagnostics (`last_flash`). Retry with the *Reflash firmware* button (disabled by default; enable it first).
* **The sensor is silent after a flash.** Give it a few minutes; the integration checks again 30 s after the flash. If it stays silent, re-insert the battery.

## Other known issues

* An update can switch off BTHome advert encryption or reset settings (see *After a flash* in [usage.md](usage.md)). Check the BTHome entry afterwards.
* pvvx sensors are connectable only part of the time; "not connectable" errors mean retry in a minute.
* PIN sensors: see [pin-sensors.md](pin-sensors.md).
