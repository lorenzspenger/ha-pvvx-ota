# Examples

Back to the [README](../README.md). Replace the entity ids with your own. The update entity of a sensor is `update.<name>`; the firmware and settings entities sit on the sensor's BTHome device.

## Update all sensors at night

The queue flashes the sensors one after another, and the battery check still protects each one.

```yaml
automation:
  - alias: "Update thermometer firmware at night"
    triggers:
      - trigger: time
        at: "03:00:00"
    actions:
      - action: update.install
        target:
          entity_id:
            - update.atc_aabbcc  # replace with your pvvx update entities
            - update.atc_ddeeff
```


## Warn when a sensor goes silent

Warning when a sensor goes silent. That is what you see in Home Assistant when advert encryption was switched off by a flash (the BTHome entry still expects the key), when a sensor lost its battery contact, or when a failed flash left it unreachable. Repair issues (*Settings → Repairs*: *firmware update failed*, *encryption not switched back on*, *settings differ after the flash*) explain the cause; this automation just makes sure you notice. The repair issues are not exposed as entities, so the sensor's own BTHome reading is the trigger:

```yaml
automation:
  - alias: "Thermometer went silent"
    triggers:
      - trigger: state
        entity_id: sensor.atc_aabbcc_temperature  # your BTHome temperature entity
        to: unavailable
        for: "00:30:00"
    actions:
      - action: persistent_notification.create
        data:
          title: "Thermometer silent"
          message: "{{ trigger.to_state.name }} has sent nothing for 30 minutes. Check Settings > Repairs for pvvx_ota issues."
```

## Tell me when an update is available

```yaml
automation:
  - alias: "Thermometer firmware update available"
    triggers:
      - trigger: state
        entity_id: update.atc_aabbcc  # your pvvx update entity
        to: "on"
    actions:
      - action: notify.notify
        data:
          title: "Thermometer firmware"
          message: "{{ trigger.to_state.name }}: V{{ trigger.to_state.attributes.latest_version }} is available (installed V{{ trigger.to_state.attributes.installed_version }})."
```

## Flash a file you downloaded

```yaml
action: pvvx_ota.flash_file
target:
  entity_id: update.atc_aabbcc
data:
  path: /config/firmware/MHO_C401_v59.bin  # the folder must be listed in allowlist_external_dirs
```

The file name must be `<board>_v<NN>.bin` for the sensor's board. See [usage.md](usage.md) for `force` (battery) and `skip_board_check`.
