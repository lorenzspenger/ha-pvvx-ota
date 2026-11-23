# PIN-protected sensors

How to use sensors that have a PIN set. Back to the [README](../README.md). Design background: [design-notes.md](design-notes.md).

## Overview

A sensor with a PIN refuses every read and write until the link is encrypted, and Home Assistant itself cannot enter the passkey.
What works: an ESPHome Bluetooth proxy pairs with the sensor once (it enters the PIN) and stores the bond in its flash. From then
on this integration connects through that proxy only, encrypts the link with the stored key before it sends anything, and the sensor
behaves like any other. Other proxies and the local adapter are never used for it (they have no bond and are refused).

### Easiest: the PIN in Home Assistant (proxy package)

Give one proxy the pairing package [`esphome/pvvx_pin_pairing.yaml`](esphome/pvvx_pin_pairing.yaml) from this repository. In that
proxy's ESPHome YAML (ESPHome 2026.10 or newer, ESP-IDF):

```yaml
packages:
  pvvx_pin_pairing: github://lorenzspenger/ha-pvvx-ota/esphome/pvvx_pin_pairing.yaml@main   # or @v0.23.0-beta.1

esp32_ble:
  max_connections: 4   # the proxy's connection slots + other ble_clients + 1 for the pairer
```

`max_connections` must leave room for one more connection than the proxy needs without the package: with the proxy's 3 slots and no other
`ble_client` that is 4. The package sets `io_capability: keyboard_only` and `auth_req_mode: sc_mitm_bond`, which apply to every pairing this
ESP32 does (peripherals without input or output still pair Just Works). You can also download the file and include it locally
(`packages: !include pvvx_pin_pairing.yaml`).

When you add a PIN sensor and it refuses the first read, the flow asks for the PIN, calls the proxy's `pair_pvvx` action (the
`esphome.<proxy>_pair_pvvx` action) with the MAC and the PIN, waits about 25 s while the proxy pairs, and reads through that proxy.
The PIN is kept in the entry (redacted in diagnostics, never logged) so that the pairing is repeated automatically if the proxy
loses the bond. The proxy that hears the sensor best and offers the action is used. If none offers it, the sensor is ignored with a
hint to come back here. The package adds two diagnostic entities to the proxy: *pvvx pairer busy* and *pvvx pair result*.

### By hand: a fixed ble_client and a pair button

Without the package: put the PIN in the proxy's `secrets.yaml`, add a `ble_client` for the sensor with `on_passkey_request` →
`ble_client.passkey_reply` (`passkey: !secret ...`) and a template button that connects, waits 20 s and disconnects, with the same
`esp32_ble` settings. Press the button, then add the sensor in Home Assistant: the flow finds the bond (a PIN-bonded entry without a
stored PIN; a lost bond then needs the button again).

### Things to know

* The bond lives in the proxy's flash (a flash erase or NVS wipe loses it, and so does a PIN change or a reset on the sensor). Every
  connection to a PIN-bonded sensor first proves the link with a config read. If that fails, the sensor is paired again through
  its proxy with the PIN stored in the entry; if no PIN is stored, Home Assistant asks for it (*Settings > Devices & services*
  shows the sensor as *needs reconfiguration*, the dialog asks for the PIN, pairs through a proxy that offers `pair_pvvx` and
  stores it). A *pairing lost* repair issue only remains when a stored PIN did not help. Sensors added from an existing bond have no
  stored PIN: open the entry's three-dot menu, *Reconfigure*, to store it now, before any bond is lost.
* If Home Assistant picks a different proxy for the connection, the operation fails with "not reachable via the paired proxy" instead
  of trying. The first command is delayed by up to about 3 s while the link is being encrypted.
* Calling `esphome.<proxy>_pair_pvvx` puts the PIN into the call's service data, which Home Assistant's event bus and the proxy's
  API carry (encrypted between Home Assistant and the proxy). It is not logged by this integration; do not enable event or service
  debug logging while pairing.

## Known issue: the sensor refuses after a correct pairing

"Could not be paired with its PIN": the proxy log shows the passkey request and `auth complete`, the sensor still answers with insufficient
authentication. This is a problem on the sensor (an old pairing entry in its small bond table, or old firmware), and other proxies do not help.
Checklist with [TelinkMiFlasher](https://pvvx.github.io/ATC_MiThermometer/TelinkMiFlasher.html), near the sensor:

1. Connect and update the firmware to the latest version.
2. Set the PIN to 0 and save, then set your PIN again (this clears the sensor's pairing memory).
3. Add the sensor here again.

A PIN sensor can only be served by the proxy that holds its bond; if Home Assistant prefers another proxy the operation fails with
"not reachable via the paired proxy" until the paired proxy scores best again.
