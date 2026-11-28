# Firmware versions whose config layout was compared (`VERIFIED_VERSIONS`)

Source: https://github.com/pvvx/ATC_MiThermometer, compared 2026-11-23 (read-only, nothing written to a sensor). Tags: `v5.5` = `e6dc8ca` (`#define VERSION 0x55`, src/app_config.h:9), `v5.7` = `8e8b90c`; 5.8 = `9afe074`, 5.9 = `9b65a24` (no tags, see display-layouts.md). Line numbers below are at `v5.5`.

## 5.5 (0x55) is identical to 5.7-5.9 for everything the integration reads or writes

| Item | v5.5 | Result `git diff v5.5 v5.7` |
|---|---|---|
| `cfg_t` (src/app.h:14-98), `flg`/`flg2`/`flg3`, `event_adv_cnt`, adv/measure/rf/latency/step/`hw_ver`/averaging | `VERSION < 0x47` branch (:78-80) only drops flg3/event_adv_cnt below 4.7, so 5.5 has them | same bytes, same bit positions; the only hunks are `#if DEVICE_TYPE == DEVICE_LYWSD02MMC` additions (`show_battery`, `time_am_pm`, `show_day_of_week`), i.e. board 49, which is not offered display fields |
| `CMD_ID_CFG` 0x55 get/set (src/cmd_parser.c:340-372): `memcpy(&cfg, &req->dat[1], len)`, `test_config()`, flash write, reply `ble_send_cfg()` | read-modify-write works the same | only change: a LYWSD02MMC-only `display_cmp_buff` clear and LCD-update macro |
| `CMD_ID_CFS` 0x25 sensor coefficients (cmd_parser.c:683-720), `sensor_coef_t` (sensor.h, 12 bytes), `sensor_cfg_send_size 18` | same | handler text md5-identical; first 18 bytes of `sensor_cfg` (coef, id, i2c_addr, sensor_type) unchanged |
| `CMD_ID_UTC_TIME` 0x23 (cmd_parser.c:565-580), payload = 4 byte UTC | same | no hunk |
| `CMD_ID_PINCODE` 0x70 (cmd_parser.c:520) | same | no hunk |
| Display flag use per driver (`cfg.flg*` in src/epd_*.c, src/lcd_*.c), md5 of the grep without line numbers | | identical at 5.5 and 5.7 |

Example board: hardware id 7 = CGG1-M 2022 (`DEVICE_CGG1` = 7, app_config.h:19-21), e-ink, `epd_cgg1n.c`. Integration offers only `temp_f` and `screen_off`:
- `temp_f`: `lcd()` in src/lcd.c is compiled for this board (lcd.c:10 excludes only MJWSD05MMC at 5.5), tests `cfg.flg.temp_F_or_C` at :215 and calls `show_temp_symbol(TMP_SYM_F 0x60 / TMP_SYM_C 0xA0)` (lcd.h:47-48); `epd_cgg1n.c:124-136` handles bits 0x20/0x40/0x80.
- `screen_off`: `epd_cgg1n.c:358,395`, `epd_cgg1.c:342,362`; `app.c:1209`. `epd_cgg1n.c` has no diff between v5.5 and v5.7.

TelinkMiFlasher: the page is not versioned per release (display-layouts.md); it treats every fw >= 4.8 the same (`hwver_id` is the raw `hw_ver` byte), so no 5.5-specific branch exists for these fields.

Decision: 0x55 added to `VERIFIED_VERSIONS`; no per-version capability exclusion needed. 0x56 is verified too (see the 5.6 section). Not verified on hardware yet: a real V5.5 sensor has only been read, never written.

## 5.6 (0x56): config layout identical to 5.5, in `VERIFIED_VERSIONS`; the bindkey does not survive a flash to 5.8/5.9

Compared 2026-11-27 (read-only, nothing written to a sensor). 5.6 has no tag. Nine commits have `#define VERSION 0x56` (src/app_config.h:9), found by checking `app_config.h` at every commit between `v5.5` and `8e8b90c` (5.7): `c6b5e43` (2025-12-03, "start v5.6 beta"), `1394c2e` (12-06), `57b03a8` (12-09), `418dfed` and `7af7dd3` (2026-01-05), `32a99d9` (01-16), `2564ccf` (01-22), `e531ea7` (01-24), `e65881a` (01-31). `9ab5561` (2026-02-11) already has 0x57.

| Item | `git diff v5.5 <commit> -- src` (all nine) |
|---|---|
| `cfg_t` (src/app.h), `flg`/`flg2`/`flg3`, `event_adv_cnt`, `hw_ver`, `VERSION < 0x47` branch (app.h:117) | `app.h` has no diff, so 5.6 equals 5.5 (and 5.5 equals 5.7 for the boards the integration offers fields for, see above) |
| `CMD_ID_CFG` 0x55, `CMD_ID_CFS` 0x25 (`sensor_coef_t`, `sensor_cfg_send_size`), `CMD_ID_UTC_TIME` 0x23, `CMD_ID_PINCODE` 0x70, 0x18 bindkey, 0x56 reset, 0x72 reboot (all in src/cmd_parser.c), `ble_send_cfg` (src/ble.h), `sensor.h`, `cmd_parser.h` | no diff in `cmd_parser.c`, `ble.h`, `ble.c`, `sensor.h`, `cmd_parser.h` |
| OTA path | the only `app_att.c` change (from `e531ea7`) adds the model/hardware strings of the new `DEVICE_ZBEACON2TH01` (id 47); the OTA services and the `DEV_SERVICES` of the existing boards are unchanged |
| Display flag use per driver (`cfg.flg*` in `src/epd_*.c`, `src/lcd_*.c`), md5 of the grep without line numbers | identical to 5.5 and 5.7 |

The only differences to 5.5 are: `lcd_lywsd03mmc.c` (hardware id 10 and the other LYWSD03MMC revisions: `lcd_send_spi`, `lcd_send_uart` and `init_lcd` only, i.e. detection of the SPI/UART panel at start; `cfg.flg2.screen_off` is used at lines 256/350/359 exactly as in 5.5 and 5.7), `app.c` (default config of the new `DEVICE_ZBEACON2TH01`, one `#elif` condition), `app_att.c` and `zb_version.h` (strings and the id of that new board), `app_config.h` (VERSION, the new board and a pull-up define for LYWSD03MMC pins), `sensors.c` (a comment). Boards 7 (CGG1-M) and 12/13 (MJWSD05MMC/06MMC) have no diff at all. The new board (id 47, no display, `USE_EPD 0`) is not in `DISPLAY_BY_HW`, so it gets no display fields.

Version-dependent exclusions (`VERSION_EXCLUDED`: `screen_off` below 4.3, `not_day_of_week` below 4.9, `date_ddmm` below 5.4) and `capabilities_for` need no change: 5.6 is above all thresholds and has the 5.5 layout.

### Bindkey and settings do not survive a flash from 5.6 (or 5.7, or 5.5) to 5.8/5.9

Found 2026-11-27 after a real sensor went from a 5.6 beta build to 5.9 with BTHome encryption on: the flag was off afterwards and switching it on with `keep_key` made Home Assistant's BTHome report "Decryption failed". Compared `e65881a` (5.6), `v5.7` and `9b65a24` (5.9), read-only:

- **Same in 5.6 and 5.9:** command 0x18 (`CMD_ID_BKEY`, cmd_parser.c:475-488: 16 bytes written to `bindkey` + `flash_write_cfg(&bindkey, EEP_ID_KEY, 16)`, then read back from the EEP, `0xff` if missing); `bindkey_init()` (app.c: `flash_read_cfg(bindkey, EEP_ID_KEY, 16)`, if absent `generateRandomNum` + write, no per-device or MAC derivation); BTHome v2 encryption (bthome_beacon.c: nonce = MAC (swapped public MAC) + UUID 0xFCD2 + info byte `BtHomeID_Info_Encrypt` + 32-bit counter, `aes_ccm_encrypt_and_tag(bindkey, ...)`, 4 byte MIC). The only bthome_beacon.c/.h/ccm.c hunks are new sensor fields (illuminance, BMP280 second temperature). The key and the cipher are not the problem.
- **Different:** the EEPROM storage. 5.5-5.7 (`flash_eep.h`): 16-bit ids (`EEP_ID_KEY 0xBEAC`, `EEP_ID_CFG 0x0CFC`, `EEP_ID_VER 0x5555`), 4 byte record header (u16 size, u16 id). 5.8 (`9afe074`, 2026-05-04, "v5.8", the only commit touching `flash_eep.c/.h` after 5.5) rewrote it as "EEP Version 2.0": ids are an enum (`EEP_ID_VER` = 0 ... `EEP_ID_KEY` = 14), 1 byte id and 1 byte size per record. 5.8 and 5.9 are identical in this file.
- **Effect:** at the first start of 5.8/5.9 `flash_supported_eep_ver()` does not find its version record in the new format and calls `flash_erase_all_cfg()` (all four 4 KiB sectors at 0x7C000 are erased; no migration code exists). Everything stored in the EEP is lost and the defaults apply: config block (`flg2` encryption flag off), bindkey (`bindkey_init` writes a NEW random key), pincode, trigger, comfort, time adjust, sensor coefficients. A key stored by 5.6 does NOT survive a flash to 5.8/5.9; the sensor then holds an unknown random key, so `keep_key` switches encryption on with a key that matches no BTHome entry.
- The same break exists for 5.5 -> 5.8/5.9 and 5.7 -> 5.8/5.9 (already in `VERIFIED_VERSIONS`), and for a downgrade 5.8/5.9 -> 5.7 or older (the old code does not find its records either; `flash_supported_eep_ver` erases when the stored value is below the minimum or missing).
- The integration's assumption "a flash clears the encryption flag but the key stays on the sensor" (store.py `_restore_encryption`, `require_stored_key`) is therefore wrong across the 5.7/5.8 boundary. Needed before 0x56 can be added: after a flash whose source is < 5.8 and target >= 5.8 (or the reverse), do not use `keep_key`; either write the key again (it is not kept by design) or tell the user the key is new and has to be entered in the BTHome entry, and restore the snapshot settings (they were reset to defaults too).

Decision: 0x56 added to `VERIFIED_VERSIONS` (2026-11-27) together with the cross-boundary handling: the integration reads the key before a flash across 5.7/5.8 (only if encryption is on), keeps it in memory, writes it back after the version check and writes the snapshot settings back; if that fails it raises the `encryption_key_replaced` repair issue. Sensor coefficients (command 0x25, firmware 4.8 and newer on both sides, only if they differ from the factory values) are carried over the same way, written after the key and before the settings; the PIN is not carried over.

---

# Older versions (research 2026-11-23, nothing implemented yet)

Method as above, on every tag of pvvx/ATC_MiThermometer (V1.1 … v5.7), via `git show <tag>:src/app.h` (`cfg_t`), `src/cmd_parser.c` (0x55, 0x25, 0x23 handlers), `src/ble.h` (`ble_send_cfg`), and `grep cfg.flg*` over `src/epd_*.c`/`src/lcd_*.c` per board. Version byte = `#define VERSION` in `src/app_config.h` at the tag. Oddities: tag `v3.6` reports 0x37 (the 0x36 bump is `7749252` "start v3.6", 2022-02-17); 0x40 only exists as `e2c611e` (beta v4.0, 2023-01-20); 5.6 only as beta commits `c6b5e43` (2025-12-03) .. `9ab5561` (2026-02-11), no tag; 5.8 = `9afe074`, 5.9 = `9b65a24`.

Common to every version >= 2.1: the reply is `0x55, VERSION, cfg_t bytes (11), pad` (`app.c:159-161` in v2.1, `ble.h` `ble_send_cfg`), the write payload is `0x55` + cfg bytes (`memcpy(&cfg, &req->dat[1], len)`), `CMD_ID_UTC_TIME` 0x23 takes 4 bytes UTC (`cmd_parser.c`, v2.1 on), 0x55 = CMD_ID_CFG in `cmd_parser.h` at all tags. The flag bytes of the common part (flg bits 0-2, 4-7, flg2 bits 0-3) are the same from 3.7 on.

Byte map (index from the 0x55 byte): 2 flg, 3 flg2, 4-5 see column, 6 adv interval, 7 measure interval, 8 rf power, 9 latency, 10 step, 11 hw, 12 averaging.

| Version(s) | idx 4-5 | idx 11 (hw) | 0x25 sensor coef | flg2 bits 5-7 | Group |
|---|---|---|---|---|---|
| 5.4 (`39acc6a`), 5.5, 5.6 beta (`c6b5e43`), 5.7-5.9 | flg3 (incl. `date_ddmm`), event_adv_cnt | raw `hw_ver` | 0x25 + 18 bytes | bt5phy, longrange, screen_off | **(a)** |
| 5.1, 5.2, 5.3 (`008e509`..`f06c77f`) | flg3 (no date_ddmm: bit 6 reserved, app.h v5.3), event_adv_cnt | raw | 18 | same | **(b)** exclude `date_ddmm` |
| 5.0, 4.9 (`04852a0`, `1a5ffdf`) | `u8 flg3, flg4` reserved; weekday = `MASK_FLG3_WEEKDAY 0x80` on the flg3 byte (app.h:44/45, lcd_mjwsd05mmc.c:717,875) = same bit as `not_day_of_week` | raw | 18 | same | **(b)** exclude `date_ddmm` (hw 13 does not exist yet: `lcd_mjwsd06mmc.c` first appears at 5.1) |
| 4.8 (`24a07ed`) | `x1, x2` reserved, no flg3 use anywhere | raw | 18 | same | **(b)** exclude `date_ddmm`, `not_day_of_week`, hw 12/13 do not exist (`lcd_mjwsd05mmc_en.c` first at 4.9) |
| 4.7 (`28a50ef`) | `x1, x2` reserved | `hw_cfg`: hwver low nibble 0-8, bit 7 = shtc3 (app.h v4.7) | 0x25 exists, reply is 17 bytes (`thsensor_cfg_send_size 17`, sensor.h:30) | same | **(b)** hw id = byte & 0x0F, calibration reply length 18 not 19 |
| 4.3-4.6 | `s8 temp_offset, s8 humi_offset` (0.1 units, range -125..125: sensors.c:203-207 `cfg.temp_offset * 10` in 0.01) in the block, no 0x25 | `hw_cfg` | none | same | **(b)** offsets in the block only (new fields, verify first), hw id masked |
| 4.2 (`8863530`) | offsets in the block | `hw_cfg` | none | bit 7 reserved, no `screen_off` | **(b)** also exclude `screen_off` |
| 4.1 and older (>= 2.1) | offsets | hw_cfg (3.6+: hwver 4 bits; 2.1-3.5: 3 bits + clock/memo/trg) | none | 4.1: bt5phy 5, ext_adv 6, **longrange 7**; 3.6-3.9: bt5hgy 5, chalg2 6, bit 7 reserved | **(c)** |
| 1.1, 1.4 | `smiley` is a separate byte, no flg2 | - | none | - | **(c)** |

Why 4.1 and older stay out: the layout of flg2 differs (longrange sits on bit 7, which is `screen_off` later), `ble_send_cfg` does not refresh `my_RxTx_Data` before v4.2 (ble.h v4.1 has no `memcpy`, v4.2 has, commit `dabb69f`), so a read after a change can return stale bytes and the integration's echo check would be meaningless, and MJWSD05MMC (x100, screen_type) does not exist before 4.2.

Display boards per version: hw ids come from the sensor, so a board only appears on versions that have its driver (hw 12 `lcd_mjwsd05mmc_en.c` from 4.9, hw 13 `lcd_mjwsd06mmc.c` from 5.1, with `screen_off` use already there). Fields stay as in `DISPLAY_BY_HW` except the exclusions in the table.

## What the code does with this (v0.18.0)

- `VERIFIED_VERSIONS` = 4.2 .. 5.5 (every tag), 5.6 (beta commits, compared), 5.7, 5.8, 5.9. Refused: 4.1 and older (group c), anything newer than 5.9.
- `config.VERSION_EXCLUDED`: `screen_off` below 4.3, `not_day_of_week` below 4.9, `date_ddmm` below 5.4. Everything else uses the same bytes and bits in all of 4.2 .. 5.9 (display flag uses compared per board file, see above). The hardware id was already decoded per version in `ota.parse_cfg` (low nibble below 4.8).
- Bytes 4-5 are never touched: read-modify-write writes the freshly read block back, so the in-block temperature/humidity offsets of 4.2 .. 4.6 are preserved. They are not offered as entities (verified in sensors.c:203-207 `cfg.temp_offset * 10`, s8 in 0.1 units, but there is no entity for them yet).
- Calibration (command 0x25, factory tables identical at every tag 4.9 .. 5.5, 4.8 lacks only the CHT8215 table) is only read and offered from 4.8 (`calib.MIN_VERSION`); 4.7 replies with 17 bytes and no chip type, older versions do not know the command.
