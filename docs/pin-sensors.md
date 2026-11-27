# PIN-protected sensors

> **Experimental, not stable.** This repository supports pvvx sensors **without a PIN**. PIN sensors work only through the workaround below (an ESPHome package on one proxy) and can fail on some sensors: pairing succeeds, the link is encrypted, and the sensor still refuses. If that happens, use TelinkMiFlasher: update the firmware, set the PIN to 0, save, then set the PIN again (checklist at the end). For reliable use, remove the PIN.

How to use sensors that have a PIN set. Back to the [README](../README.md). Design background: [design-notes.md](design-notes.md).

## Overview

A sensor with a PIN refuses every read and write until the link is encrypted, and Home Assistant itself cannot enter the passkey. What works: an ESPHome Bluetooth proxy pairs with the sensor once (it enters the PIN) and stores the bond in its flash. From then on this integration connects through that proxy only, encrypts the link with the stored key before it sends anything, and the sensor behaves like any other. Other proxies and the local adapter are never used for it (they have no bond and are refused).

### Easiest: the PIN in Home Assistant (proxy package)

Give one proxy the pairing package [`esphome/pvvx_pin_pairing.yaml`](../esphome/pvvx_pin_pairing.yaml) from this repository. In that proxy's ESPHome YAML (ESPHome 2026.10 or newer, ESP-IDF):

```yaml
packages:
  pvvx_pin_pairing: github://lorenzspenger/ha-pvvx-ota/esphome/pvvx_pin_pairing.yaml@main   # or a release tag: @v<release>

esp32_ble:
  max_connections: 4   # the proxy's connection slots + other ble_clients + 1 for the pairer
```

Two ways to reference the package:

* `@main`: always the current file; changes to the package arrive with every compile of the proxy, independent of the integration version.
* `@v<release>` (a release tag, for example `@v0.26.0`): a fixed file that changes only when you change the tag. After an integration update that changes the package, raise the tag; the integration's *pairing package out of date* repair issue says when.

`max_connections` must leave room for one more connection than the proxy needs without the package: with the proxy's 3 slots and no other `ble_client` that is 4. The package sets `io_capability: keyboard_only` and `auth_req_mode: sc_mitm_bond`, which apply to every pairing this ESP32 does (peripherals without input or output still pair Just Works). You can also download the file and include it locally (`packages: !include pvvx_pin_pairing.yaml`).

When you add a PIN sensor and it refuses the first read, the flow asks for the PIN, calls the proxy's `pair_pvvx` action (the `esphome.<proxy>_pair_pvvx` action) with the MAC and the PIN, waits for the proxy to pair (30 to 40 seconds), and reads through that proxy. The PIN is kept in the entry (redacted in diagnostics, never logged) so that the pairing is repeated automatically if the proxy loses the bond. The proxy that hears the sensor best and offers the action is used. If none offers it, the sensor is ignored with a hint to come back here. The package adds three diagnostic entities to the proxy: *pvvx pairer busy*, *pvvx pair result* and *pvvx package version*. The integration finds them by the names the package gives them (renaming one in Home Assistant is fine). If the version is missing or lower than the integration expects, pairing still runs, one warning is logged per proxy and the *pairing package out of date* repair issue appears; it clears when a later pairing finds the current version. A run that hangs is stopped by the package after 90 s (result `timeout`); the integration treats that like *no connection* and retries once.

When the sensor's entry is removed from Home Assistant, the integration calls the proxy's `forget_pvvx` action (package from v0.23.0-beta.3) so that the bond does not stay in the proxy's flash and in the sensor's small bond table. A proxy with an older package has no such action: the bond stays until you update the package or erase the proxy's flash, and the integration logs that at INFO.

### Experimental rescue: `pvvx_clear_pin`

> **Experimental.** For a sensor whose pairing is broken: the pairing succeeds but the sensor refuses every later encrypted link, and TelinkMiFlasher is not at hand. To remove the PIN of a sensor whose pairing works, use `pvvx_ota.set_pin` with 0.

The package offers a second action, `pvvx_clear_pin(mac, pin)`. The proxy pairs with the sensor using the PIN, writes the clear-PIN command on that freshly authenticated link and disconnects; the sensor then has no PIN and an empty pairing table. Call it from *Developer tools > Actions* (YAML mode), with the node name of your proxy:

```yaml
action: esphome.<node>_pvvx_clear_pin
data:
  mac: "AA:BB:CC:DD:EE:FF"
  pin: 123456
```

The result shows in the proxy's *pvvx pair result* entity: `pin clear sent` (the command was written on an open link), `clear not sent` (the link dropped before the write) or `timeout`. Afterwards add the sensor to Home Assistant without a PIN, and set one again with `pvvx_ota.set_pin` if you want it.

### By hand: a fixed ble_client and a pair button

Without the package: put the PIN in the proxy's `secrets.yaml`, add a `ble_client` for the sensor with `on_passkey_request` → `ble_client.passkey_reply` (`passkey: !secret ...`) and a template button that connects, waits 20 s and disconnects, with the same `esp32_ble` settings. Press the button, then add the sensor in Home Assistant: the flow finds the bond (a PIN-bonded entry without a stored PIN; a lost bond then needs the button again).

### Things to know

* **A flash across firmware 5.7/5.8 (either direction) erases the PIN and the sensor's pairing table.** According to the firmware source (not yet seen on hardware) the whole EEPROM is erased by the storage rewrite of 5.8. The proxy keeps its half of the bond, but the sensor is unprotected afterwards. Set the PIN again with `pvvx_ota.set_pin`; on a PIN-bonded entry the integration stores it and pairs again through the bonded proxy by itself (a *pairing lost* issue appears if that fails: *Reconfigure* on the entry).

* The bond lives in the proxy's flash (a flash erase or NVS wipe loses it, and so does a PIN change or a reset on the sensor). Every connection to a PIN-bonded sensor first proves the link with a config read. If that fails, the sensor is paired again through its proxy with the PIN stored in the entry; if no PIN is stored, Home Assistant asks for it (*Settings > Devices & services* shows the sensor as *needs reconfiguration*, the dialog asks for the PIN, pairs through a proxy that offers `pair_pvvx` and stores it). A *pairing lost* repair issue only remains when a stored PIN did not help; then *Reconfigure* the entry (enter the PIN again) or, with a hand-made setup, press the proxy's pair button. Sensors added from an existing bond have no stored PIN: open the entry's three-dot menu, *Reconfigure*, to store it now, before any bond is lost.
* **Setting or clearing the PIN with `pvvx_ota.set_pin`.** The sensor erases its bonds when its PIN changes. On a PIN-bonded entry a new PIN is stored in the entry and the sensor is paired again through the bonded proxy; if that fails, the *pairing lost* issue asks you to *Reconfigure* the entry. Clearing the PIN (0) turns the entry into a plain one again and asks the proxy to forget the bond (`forget_pvvx`). On an entry without a bond (a PIN set after the sensor was added, or with another tool) the *PIN was set* issue appears: open the entry's three-dot menu, *Reconfigure*, enter the PIN, and the sensor is paired through a proxy that offers `pair_pvvx` (the entry, its options and settings stay). Without such a proxy *Reconfigure* stops with a hint.
* If Home Assistant picks a different proxy for the connection, the operation fails with "not reachable via the paired proxy" instead of trying. The first command is delayed by up to about 3 s while the link is being encrypted.
* **Where the PIN goes.** Calling `esphome.<proxy>_pair_pvvx` (and the `pvvx_ota.set_pin` action) puts the PIN into the call's service data. Home Assistant core copies that data into a `call_service` event on its event bus (checked in `homeassistant/core.py`: `ServiceRegistry.async_call` fires `EVENT_CALL_SERVICE` with `service_data`). Anything that subscribes to events (an admin's websocket session, debug logging of events) can see it, and the recorder stores events unless they are excluded, so it can end up in the database until the purge removes it. The integration itself never logs it and keeps it only in the config entry (redacted in diagnostics). There is no supported way around the event for an action call; to keep it out of the database add `recorder: exclude: event_types: [call_service]` to your configuration, and do not enable event or service debug logging while pairing. The proxy keeps the PIN only in RAM during the pairing (zeroed afterwards); the connection between Home Assistant and the proxy is encrypted when the API uses encryption.

## Known issue: the sensor refuses after a correct pairing

"Could not be paired with its PIN": the proxy log shows the passkey request and `auth complete`, the sensor still answers with insufficient authentication. This is a problem on the sensor (an old pairing entry in its small bond table, or old firmware), and other proxies do not help. Checklist with [TelinkMiFlasher](https://pvvx.github.io/ATC_MiThermometer/TelinkMiFlasher.html), near the sensor:

1. Connect and update the firmware to the latest version.
2. Set the PIN to 0 and save, then set your PIN again (this clears the sensor's pairing memory).
3. Add the sensor here again.

A PIN sensor can only be served by the proxy that holds its bond; if Home Assistant prefers another proxy the operation fails with "not reachable via the paired proxy" until the paired proxy scores best again.
