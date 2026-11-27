# Can a pvvx sensor be flashed with an older firmware? (research 2026-11-27)

Source research only: github.com/pvvx/ATC_MiThermometer cloned read-only, nothing was flashed or written to a sensor, none of this was tested on hardware. Evidence is cited as `<commit>:file:line`. `HEAD` = `40f00f7` (2026-11-23, still `#define VERSION 0x59`). Tags exist for 4.2 to 5.5 and 5.7; 5.6 (beta commits), 5.8 (`9afe074`) and 5.9 (`9b65a24`) have none (see version-layouts.md).

## Verdict

**Downgrades are possible from the firmware's side, on every board, with conditions.** The sensor-side OTA does not compare versions, has no anti-rollback and does not check the board. The only gates are the Telink signature, the image size and an image checksum. Everything that can go wrong sits around the OTA, so the integration must do these checks itself before it allows one:

1. **Board**: the image must be built for this sensor. The firmware never checks it (a wrong image is accepted and runs), and from 4.6 on the image header carries the board byte (below), which is a stronger check than the file name.
2. **Target supports this hardware id**: an older release may not know the hardware variant (hw id 14 does not exist before 5.3; boards are missing before the version that introduced them, table below).
3. **Size** below 0x20000 (every image 4.2 to 5.9 is 73 to 87 KB, so this holds, but check it anyway).
4. **Exact build**: file names are reused for rebuilds, and the "5.9" binary at `master` is not the one released in `9b65a24`. Pin the commit and the blob hash, never fetch by name from `master`.
5. **EEPROM consequences per boundary crossed** (settings, bindkey): see the table in section 4.

## 1. The OTA receive path

| Finding | Evidence |
|---|---|
| The receiver is a prebuilt closed library (`otaWrite`, `otaRead`, `ota_save_data`, `ota_set_flag`, `bls_ota_*` are only symbols in `liblt_8258.a`; only the header `ble_ll_ota.h` is source). The library file has not changed since `884d8f8` (2022-02-03, "add SDK"), i.e. the same receiver is in every version 4.2 to 5.9. | `git log -- SDK/components/proj_lib/liblt_8258.a` lists that one commit; `strings liblt_8258.a` |
| The Telink protocol has a version request, `CMD_OTA_FW_VERSION 0xff00` (what the integration sends first as `00ff`, ota.py:408), and a callback hook `bls_ota_registerVersionReqCb` for it. **The firmware never registers it**, in any version: the only registrations are `bls_ota_registerStartCmdCb(app_enter_ota_mode)` (ble.c:756) and the result callback (ext_ota.c:280). Checked at v4.2, v4.5, v4.7, v5.0, v5.3, v5.5, v5.7, `9afe074`, `9b65a24`, HEAD: zero hits for `registerVersionReqCb`. | `HEAD:SDK/components/stack/ble/service/ble_ll_ota.h:39,95`; `HEAD:src/ble.c:755-756` |
| The start callback `app_enter_ota_mode` only sets `wrk.ota_is_working = OTA_WORK`, shows the OTA screen, sets latency 0 and a 16 s timeout. No check on the image, no version. `otaWritePre` only refreshes the timeout tick and forwards to `otaWrite`. | `HEAD:src/ble.c:63-74,184-186` |
| Library error codes: success, packet loss, data CRC, flash write, incomplete, timeout, `OTA_FW_CHECK_ERR` (firmware check). Nothing for "version" or "board". | `ble_ll_ota.h:79-89` |
| Header: bytes 0-3 jump, 4-7 `FILE_VERSION` (Zigbee; byte 6 `5D` = `FLAG_FW_CHECK`, byte 7 `02` = `FW_CHECK_AGTHM2`), 8-11 `KNLT` (0x544c4e4b), 0x0C `0x00880000 + size/16`, 0x14 Zigbee manufacturer 0x1141, 0x16 `IMAGE_TYPE`, 0x18 size = payload + 4. Every image 4.2 to 5.9 has `5D 02`, so the library's firmware check (an image checksum) applies to all of them; its exact algorithm is in the closed library and was not read. The integration already checks `KNLT` and the 0x18 size (ota.py:165-173). | `HEAD:src/cstartup_825x.S:65-88`; `HEAD:SDK/.../ble_ll_ota.h:44-45`; byte dump of ATC images at v4.2, v5.3, v5.5, v5.7, 5.9 |
| `IMAGE_TYPE = (CHIP_TYPE << 8) \| DEVICE_TYPE`. **From 4.6 the header carries the board**: 0x14-0x15 = `0a 02` for ATC (LYWSD03MMC), `02 02` CGG1, `07 02` CGG1M, `09 02` BTH, `06 02` CGDK2, `25 02` ZYZTH02, and so on (all 48 `*_v59.bin` have the same first 8 bytes and a per-board 0x14). Up to 4.5 these bytes are zero (checked: `ATC_v44.bin`, `ATC_v42.bin`; first non-zero at `ATC_v46.bin`, tag v4.6). Nothing on the sensor reads this byte for BLE OTA (grep: no use of `IMAGE_TYPE` outside `zb_version.h:89` and the start-up header); it exists for Zigbee OTA. | `HEAD:src/zb_version.h:89`; `HEAD:src/cstartup_825x.S:85` |
| `pvvx`'s own command line tool decides "older" on the client only: `ble_ota.py` skips the flash when `ver >= want` unless `--force` is given, and `--file X.bin --force` flashes any image. The web flasher likewise takes any local file. | `HEAD:utils/ble_ota.py:231,285,11` |

Conclusion for point 1: no version comparison, no "allow downgrade" flag, no anti-rollback counter exists in the application code of any version, and the library exports no hook that would reject an older image. The library itself could still refuse something internally; that was not testable from source. Hardware test needed before shipping the feature (one sensor, 5.9 to 5.7 to 5.9, with a spare for recovery).

## 2. Board check

* The sensor does not refuse an image built for another board. Neither the application nor the OTA hooks look at the header's `IMAGE_TYPE` (point 1). Whether the library does is not visible in source, so treat a wrong image as accepted and possibly bricking (a wrong-pin image on other hardware; recovery would need the SWS pin or a Zigbee/Big OTA rescue file).
* **5.8 and 5.9 notice a foreign image only after the flash**: the version record in the EEPROM is `(VERSION << 16) | DEVICE_TYPE` and `flash_supported_eep_ver` requires `(stored & 0xffff) == (new & 0xffff)`; on a mismatch it calls `flash_erase_all_cfg()` (`9b65a24:src/flash_eep.c:426-441`, `src/app.c:854`). That erases settings and key, it does not stop the image. 5.7 and older store only `VERSION` (`v5.7:src/flash_eep.c:417-441`, `src/app.c:770`), so there is no board trace at all.
* The integration's check today is the file name (`board_of_filename`, firmware.py:156-169; `<BOARD>_vNN.bin` against the board of the sensor's hardware id from firmware.json). That is enough for a stable image fetched through the catalog, but a user-supplied file only gets the name test. For images from 4.6 on, add the header test: byte 0x14 of the image must equal the board's `DEVICE_TYPE`. Hardware id 0, 3, 4, 5, 10, 14 (all LYWSD03MMC revisions) share `ATC_*.bin` whose header byte is `0x0a`, which is why the sensor-reported hardware id and the image's device byte differ for them; a per-board table (hw id to header byte) is needed, taken from `DEVICE_*` in `app_config.h` (`ATC` 0x0a, `MHO_C401` 1, `CGG1` 2, `CGDK2` 6, `CGG1M` 7, `MHO_C401N` 8, `BTH` 9, `MHO_C122` 11, `BTE` 12, `MJ6` 13 and so on). Below 4.6 there is nothing to check but the name.

## 3. Size and flash layout

* All images fit the standard OTA area. Largest image in each tag's tree (`git ls-tree -l`; 5.8 and 5.9 at their commits): 4.2 85,332 B, 4.9 85,812, 5.0 85,924, 5.5 86,836, 5.7 86,724, 5.8 86,340, 5.9 86,404 (`ATC_v59.bin`, header size equals file size for all 48 boards); smallest 5.9 image 73,220 B (`TB_INA226`). The BLE OTA limit is `FW_MAX_SIZE 0x40000` in the library header but the web flasher and `ble_ota.py` limit plain BLE OTA to 0x20000 (`MAX_BLE_OTA`, ota.py:23, `ble_ota.py:34`), and the integration does the same.
* Layout comments in `app_config.h` give a second image slot at **0x20000** for LYWSD03MMC (:477), MJWSD06MMC (:2633), the other 0x20000 boards (:3821), and at **0x40000** for MJWSD05MMC / MJWSD05MMC_EN (:712) and MJWSD07MMC (:3520); the EEPROM sits at 0x7C000 on all (512 KB flash, `flash_eep.h:38,42`, identical at v5.7 `flash_eep.h:34,38`).
* The plain BLE OTA does not set the bank itself: `bls_ota_set_fwSize_and_fwBootAddr` only appears in an `#if 0` branch (ext_ota.c:271), so plain OTA uses the library defaults (the receiver alternates the two slots; which defaults it uses on the 0x40000 boards could not be read from source). Only command `0x73` (`CMD_ID_SET_OTA`, cmd_parser.c:639) moves the target to a chosen address >= 0x40000, size 4 to 207 KB, 4 KB aligned (`check_ext_ota`, ext_ota.c:240-259). The integration never sends it (ota.py:408-409 sends only `00ff`, `01ff`), so the 0x40000 path is not used by it.
* The note in the 5.9 release notes ("initial OTA download at 0x40000", README:329) concerns the relocation that **the newly started firmware** runs at boot for non-Tuya builds: `big_to_low_ota()` (app.c:843) copies a bootable image from 0x40000 to 0x0 and sets the id. The 5.9 change (`9b65a24`, ext_ota.c) rewrote the copy loop (the first block is now written inside the loop and the end address is computed from the image size) and added `flash_erase_sector(BIG_OTA2_FADDR)` (ext_ota.c:84), i.e. 5.8 and older leave the copied header at 0x40000. `c794aa2` (2026-08-16, "firmware relocation code during startup post Tuya") rebuilt every `*_v58.bin` with the same `VERSION 0x58`. It matters only for a sensor that is flashed through 0x40000 (first flash from stock firmware, Zigbee rescue files, Ext.OTA in the web flasher), and for images <= 5.8 that run that code; the effect of the missing erase on a later plain OTA was not determined from source. Treat "downgrade a sensor whose current image sits at 0x40000, or flash an <= 5.8 image through 0x40000" as untested and out of scope for the first version of the feature.
* The reverse (a newer image on an older running version) is the normal update and is already how the integration works; the receiver (library) is the same in all versions.

## 4. EEPROM across versions (settings, bindkey, PIN, coefficients)

`flash_supported_eep_ver(min, new)`:

| Running to target | Behaviour | Evidence |
|---|---|---|
| any of 4.2 to 5.7 to any of 4.2 to 5.7 (up or down) | **Kept.** The stored value is the old `VERSION` byte; the check is `stored >= EEP_SUP_VER` where `EEP_SUP_VER` is `0x09` in every version 4.2 to 5.9 (no upper bound), then the version record is overwritten with the new `VERSION`. Settings and key survive a downgrade within this range. Caveat: the *meaning* of the config bytes can differ (cfg_t bytes 4-5 are temperature/humidity offsets in 4.2 to 4.6 and `flg3`/`event_adv_cnt` from 4.7; hw byte `hw_cfg` vs `hw_ver`, version-layouts.md), so values written by a newer version can be misread by an older one; after a downgrade below 4.7 read the config again and do not write the old block back. | `v5.7:src/flash_eep.c:417-441`; `v4.2:src/flash_eep.c:418-440`; `EEP_SUP_VER` per tag via `git show <tag>:src/app_config.h` (4.2 to 5.7, `9afe074`, `9b65a24` all `0x09`) |
| 5.8 to 5.9 and 5.9 to 5.8 | **Kept** (same format; `stored >= (0x09 << 16 \| DEVICE_TYPE)` is true for either). The version record is rewritten with the new version, no downgrade rejection. | `9b65a24:src/flash_eep.c:426-441`; same at `9afe074` |
| <= 5.7 to >= 5.8 | **Erased** (new format cannot find the old version record; confirmed in version-layouts.md). | `9afe074:src/flash_eep.c:426-441`, `src/app.c:814,854` |
| >= 5.8 to <= 5.7 (the downgrade direction) | **Erased as well** by reading the old code: it looks for record id `0x5555` (`EEP_ID_VER`, `v5.7:src/flash_eep.h:31`), the 5.8 area has one-byte ids and the version record is id 0 (`9b65a24:src/flash_eep.h:15`), so the `flash_read_cfg` is not satisfied and the loop at `flash_eep.c:430-436` (v5.7) erases each sector from `FMEMORY_SCFG_BASE_ADDR` (0x7C000) to the end of flash and writes the new version. Config, bindkey (a new random key is generated at the next `bindkey_init`), PIN, trigger, coefficients are lost; defaults apply. *Source reading only, not tested on hardware.* | `v5.7:src/flash_eep.c:417-441`; `v5.7:src/app.c:770` |
| any, foreign board image, target >= 5.8 | Erased (device byte differs), see section 2. | `9b65a24:src/flash_eep.c:432-439` |
| any, foreign board image, target <= 5.7 | Not detected. | |

So the integration's existing cross-boundary handling (read key before, write after, restore snapshot) has to be applied in **both** directions around 5.7/5.8, and for a downgrade the "encryption_key_replaced" notice is the expected outcome, not an exception.

## 5. Where old images come from

* **Not on `master`**: `bin/` at HEAD holds only `*_v59.bin` (45 images plus `Original/`), and `firmware.json` there lists only the current release (`"version": 89`, decimal = 0x59; `"betaver": 96` = 0x60; v59 paths only; 5.9 json at `9b65a24` has `version 89`). There is no history in the JSON. The README table of releases (the "Firmware version history" the integration already parses) gives text, not links.
* **Git history has every release image**: file path is the repo root (`ATC_v42.bin`) up to tag 5.0 and `bin/ATC_v51.bin` from 5.1 on (moved in the 5.1 time frame; v5.0 tree has no `bin/`, v5.1 has it). Fetch by commit or tag, e.g. `https://raw.githubusercontent.com/pvvx/ATC_MiThermometer/<tag-or-sha>/bin/<BOARD>_v<NN>.bin`. File naming `<BOARD>_vNN.bin` with `NN` = BCD version byte, `BOARD` from `firmware.json` (same names the integration already parses, `board_of_filename`, ota.py:176).
* **GitHub Releases exist for v1.4 to v5.7 (not 5.6, 5.8, 5.9), but have no assets** (`gh release view v5.7 --json assets` = 0): they are tag pointers only. Use the tag as the pin where there is one; 5.6, 5.8, 5.9 only have commits.
* **Same name, different builds.** The images are overwritten in place on every rebuild. Number of distinct blobs per file name over the history (`git log --all -m --raw`): `ATC_v42.bin` 7, `ATC_v43.bin` 8, `ATC_v46.bin` 9, `ATC_v51.bin` 7, `ATC_v56.bin` 2 (`c6b5e43` 2025-12-03 and `1394c2e` 2025-12-06), `ATC_v57.bin` 1, `ATC_v58.bin` 2 (`9afe074` 2026-05-04 and `c794aa2` 2026-08-16), `ATC_v59.bin` 2 or more (`9b65a24` 2026-08-26 and the HEAD build, after later fixes such as `5c92fcf` "disabling the battery icon", `1e04429`, `56d4f4d`: `VERSION` stays 0x59 at HEAD). A tag does not necessarily contain every board's image of its own version (v4.4 tag has 8 images only, v4.5 still carries `BTH_v44.bin`), and the catalog at a tag can be stale or broken: `v5.7:firmware.json` says `"version": 85` and points hw 0 at `bin/ATC_v56.bin`, which is not in the v5.7 tree. So an old image has to be located by file name in the history, taking the blob of the release commit for that board, and pinned by commit SHA + SHA-256 of the file (shipped inside the integration or a curated JSON in its repo), not resolved at run time from `firmware.json`.
* **Which boards have an image per version** (image `<BOARD>_v<NN>.bin` exists somewhere in history; hardware id to board from `HEAD:firmware.json`):

| Board (hw ids) | First version with an image | Missing in between |
|---|---|---|
| ATC (0, 3, 4, 5, 10, 14), BTH (9), CGDK2 (6), CGG1 (2), CGG1M (7), MHO_C401 (1), MHO_C401N (8) | 4.2 (earlier ones are outside the verified range) | none |
| MHO_C122 (11) | 4.3 | none |
| BTE (12) | 4.9 | none |
| TB03F (16), TS0201 (17), TNK01 (18), TH03Z (22), ZTH01 (27), ZTH02 (28), TH03 (30), ZTH05 (33) | 4.8 | none |
| LKTMZL02 (31), ZG227Z (39), ZYZTH02 (37), ZYZTH02P (38) | 5.0 | none |
| MJ6 (13) | 5.1 | none |
| PLM1 (29) | 5.2 | none |
| ZBTH01 (45), ZG303Z (44) | 5.4 | none |
| ZB_MC (46) | 5.5 | none |
| ZB2TH01 (47) | 5.6 | none |
| LYWSD02MMC (49) | 5.7 | none |
| TS0201W (52), ZG204ZV (51) | 5.8 | none |

(Boards that are only in `firmware.json` as `?` have no BLE image at all.) Downgrading a board below its first version is impossible; the target list for a sensor must be limited to versions `>=` its first image and `>= 4.2` (the integration's `VERIFIED_VERSIONS` floor).
* **Hardware variants** need the right firmware generation: hw id 14 (`HW_VER_LYWSD03MMC_NB16`, the 2025 B1.6 panel) is absent from `v5.2:src/app.h` and present from `v5.3` (beta commit `e7a58a4`, 2025-06-24; `app.h:28`); B1.5 auto-detect came with `d9d3aa7` (2025-07-16). An image from before that cannot be assumed to drive such a sensor (display, sensor detection). Because the sensor reports its hardware id before the flash (ota.py `read_cfg`), the integration can refuse targets older than the first version that knows that id; the table of first versions per hardware id must be built from `HW_VER_*` in `app.h` per tag (only 14 was checked here, the other LYWSD03MMC ids 0, 3, 4, 5, 10 are in `v4.9`, ids 11, 12, 13 as in the board table above).

## 6. What the integration must do before allowing a downgrade (checklist)

1. Take the target only from a curated list (board, version, commit SHA, SHA-256, size), not from `master` or `firmware.json`.
2. Board: file name board equals the sensor's board; from 4.6 on, byte 0x14 of the image equals that board's device byte.
3. Target version >= first version of that board and >= first version that knows the sensor's hardware id; >= 4.2; <= current `master` release.
4. Image: `KNLT`, size == header size field, size <= 0x20000 (already done in `check_image`).
5. EEPROM: if source and target are on different sides of the 5.7/5.8 boundary, run the existing key/settings carry-over in the matching direction; the key is written back after the version check (unchanged logic), because the storage is erased both ways. For a target below 4.7 do not write the config block back unchanged (bytes 4-5 change meaning); below 4.8 no calibration command.
6. Warn that the version byte is not a unique build id (5.8 and 5.9 have two builds each), that this was not tested for 0x40000-origin sensors, and offer the previous version as the way back (the same flow in the other direction).
7. Hardware test before release: one real sensor 5.9 to 5.7 to 5.9 and 5.9 to 5.8, with the key and settings snapshot verified.

## Not covered

* The closed OTA library was not disassembled: whether it validates something beyond `KNLT`, `FW_CHECK` and the image checksum is unknown. Nothing in the application code suggests it does.
* Which slot the library picks when running from the 0x40000 layout boards (MJWSD05MMC family) was not read from source.
* No downgrade was observed on a sensor in this research.
