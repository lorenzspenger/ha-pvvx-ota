# Changelog

## 0.23.0-beta.1 (first public beta)

First public release. Git tag `v0.23.0-beta.1`, manifest version `0.23.0b1`. Install it as a pre-release (see the README).

- **Firmware update entity** per sensor: installed version read over Bluetooth, latest from pvvx's `firmware.json`, install with progress,
  release notes built from pvvx's version history, optional beta firmware.
- **Flashing over any Bluetooth path** Home Assistant has (ESPHome proxies or local adapters), the image chosen for the sensor's own
  hardware id, checked before sending (board, size, signature, CRC), one operation at a time, queue for *Update all*.
- **Faster, safer flashes**: fast connection parameters requested through the proxy, a slow-link check that stops the flash before the
  end marker (the sensor keeps its old firmware), a weak-signal warning, a battery check.
- **Sensor settings** (verified firmware 4.2 to 5.9): advertising and measure interval, TX power, display options per board, calibration
  offsets, clock sync, reboot, refresh; settings are saved before a flash and can be restored afterwards. Most entities are disabled by default.
- **Opt-in dangerous settings**: BTHome encryption and bindkey (`set_encryption`), PIN (`set_pin`), advertising type, PHY, reset to defaults.
- **PIN-protected sensors** through an ESPHome proxy with the `esphome/pvvx_pin_pairing.yaml` package: the config flow asks for the PIN and
  pairs through the proxy's `pair_pvvx` action; the bond is repaired automatically when the stored PIN is still known.
- Repair issues for failed flashes, lost pairing, encryption not restored, settings that differ after a flash.
- Diagnostics download, English and German translations, quality scale Silver.
