# How data is updated

Back to the [README](../README.md).

The integration does not poll in a loop and keeps no connection open. Sensor readings (temperature and so on) stay with the BTHome integration; this integration only reads the firmware version and the configuration over a Bluetooth connection when needed.

| What | When | How |
|---|---|---|
| Installed firmware version | at startup, every 3 days by default (option: every day, every 7 days or never), after a flash and on *Refresh* or `homeassistant.update_entity` | one Bluetooth connection through the best proxy or adapter |
| Sensor configuration | in the background right after startup (only if a settings entity is enabled), after a flash, after each change and on *Refresh configuration* | the same connection as above |
| Latest firmware and release notes | at startup and every 12 hours, never more often than every 10 minutes | HTTPS from pvvx's GitHub repository; the last good copy is stored and used when GitHub cannot be reached |
| After a flash | no connection for 30 s, then the new version is read (8 attempts, 30 s apart) | one Bluetooth connection |

Operations on sensors run one at a time; a second one waits in a queue. Setting up an entry makes no connection: it only checks that a scanner has heard the sensor.
