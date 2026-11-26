# Display bit layouts per board

Evidence for `DISPLAY_BY_HW` in `custom_components/pvvx_ota/config.py`. Re-check against pvvx's source before adding a board or firmware version.

Sources: https://github.com/pvvx/ATC_MiThermometer (no git tags v5.8/v5.9 exist, only v5.7). Commits used:
- 5.7 = tag v5.7 = `8e8b90c`; 5.8 = `9afe074` (commit "v5.8"); 5.9 = `9b65a24` (first commit with `#define VERSION 0x59`).
- `cfg_t` text (src/app.h) is byte-identical at all three after masking the MJWSD07MMC (id 62) additions; the `cfg.flg*` uses in lcd.c, lcd_mjwsd05mmc.c, lcd_mjwsd05mmc_en.c are identical at all three (md5 of the grep).
- Web flasher: https://pvvx.github.io/ATC_MiThermometer/TelinkMiFlasher.html fetched 2026-11-23 (not versioned per release).

All line numbers below are at commit 9b65a24 (5.9).

## cfg_t, src/app.h
- flg (byte index 2 of the 0x55 reply): advertising_type bits0-1 (:55), comfort_smiley bit2 0x04 (:56), bit3 0x08 = `x100` on MJWSD05MMC(_EN) (:58) / `show_time_smile` (clock/blink) on every other board (:62; LYWSD02MMC: show_battery), temp_F_or_C bit4 0x10 (:64), bit5 0x20 = `time_am_pm` on MJWSD05MMC(_EN)/LYWSD02MMC (:66) / `show_batt_enabled` otherwise (:68), tx_measures 0x40 (:70), lp_measures 0x80 (:71).
- flg2 (index 3): bits0-2 = `screen_type` on MJWSD05MMC(_EN) (:109) / `smiley` otherwise (:111); adv_crypto 0x08, adv_flags 0x10, bt5phy 0x20, longrange 0x40, screen_off 0x80 (:113-117).
- flg3 (index 4, since 4.7): adv_interval_delay bits0-3, reserved bits4-5, `date_ddmm` bit6 0x40 (:127, "MJWSD05MMC en"), bit7 0x80 = `not_day_of_week` (:131; LYWSD02MMC: show_day_of_week, NOT inverted). (:125-133)
- screen_type values: src/lcd.h:327-332 (SCR_TYPE_ENUM 0 Time, 1 Temp, 2 Humi, 3 Batt %, 4 Batt V, 5 External). The sensor's button cycles 0..5 (app.c:1126).

## Board -> hardware id -> DEVICE_TYPE  (src/app.h:13-27 HW_VER enum, src/app_config.h:14-90, flasher `hw_version_str` 199-)
- 0,3,4,5,10,14 = DEVICE_LYWSD03MMC (app_config.h:26-32 lists the hw_ver values). Generic bit layout; display driver src/lcd.c + lcd_lywsd03mmc.c. Uses: show_batt_enabled lcd.c:96,157,208,212; comfort_smiley :192,200; smiley :195,203; temp_F_or_C :219; screen_off :38,65.
- 9 = MJWSD05MMC (ch): lcd_mjwsd05mmc.c uses screen_off :184,226,800; time_am_pm :659,691,877; screen_type :803; temp_F_or_C :814,834,842,851,861,869; x100 :815,820,829; comfort_smiley :885; not_day_of_week :890 (date_ddmm only in a comment).
- 12 = MJWSD05MMC (en): lcd_mjwsd05mmc_en.c same lines +-1, and :889 `show_data_s2(not_day_of_week, date_ddmm)` (date_ddmm set = DD/MM, :740-749).
- lcd.c is compiled for every screen board EXCEPT MJWSD05MMC(_EN)/LYWSD02MMC/MJWSD07MMC (lcd.c:10).
- 17 = TS0201: DEV_SERVICES blocks (app_config.h:936-977) contain no SERVICE_SCREEN (no display); flasher SendCustomConfig has no display branch for 17.
- 1 MHO-C401 (e-ink, epd_mho_c401.c), 8 MHO-C401N (e-ink, epd_mho_c401n.c), 2/7 CGG1 (e-ink, epd_cgg1.c / epd_cgg1n.c), 6 CGDK2 (lcd_cgdk2.c), 11 MHO-C122 (lcd_mho_c122.c), 13 MJWSD06MMC (lcd_mjwsd06mmc.c): same flg/flg2 positions as LYWSD03MMC (app.h #else branches, flasher "else" branch). ONLY `temp_f` (0x10 in flg) and `screen_off` (0x80 in flg2) are offered:
  - temp_f: the generic `lcd()` in lcd.c:219 is compiled for all of them (lcd.c:10) and sends TMP_SYM_F 0x60 / TMP_SYM_C 0xA0 (lcd.h:53-54) to `show_temp_symbol()`; every board's implementation handles all of 0x20/0x40/0x80 (epd_mho_c401.c:85-98, epd_mho_c401n.c:108-121, epd_cgg1.c:86-99, epd_cgg1n.c:122-135, lcd_cgdk2.c:213-226, lcd_mho_c122.c:151-159, lcd_mjwsd06mmc.c:162-170), so the °F glyph (segments 0x20 + 0x40) exists on each.
  - screen_off: tested in each board's display code: epd_mho_c401.c:320, epd_mho_c401n.c:345, epd_cgg1.c:360, epd_cgg1n.c:356 (update_lcd stops: an e-ink keeps its last picture, it does not go blank), lcd_cgdk2.c:137,167, lcd_mho_c122.c:100,129, lcd_mjwsd06mmc.c:125,145.
  - Not offered: comfort_smiley/show_battery/smiley (glyph sets differ per board, SHOW_SMILEY is 0 on CGDK2: lcd.h:172, CGG1 has only a "line"). Not verified on hardware (only the maintainer's MJWSD05MMC and LYWSD03MMC were tested).
- 49 LYWSD02MMC: show_battery at 0x08, weekday NOT inverted -> excluded, different positions.

## Flasher cross-check (TelinkMiFlasher.html, 2026-11-23)
- hwver_id for fw >= 4.8 is the raw hw_ver byte (:1163); 9/12 branch at :3759-3764 (am/pm, x100, ddmm only for 12), :3770 screen type list, :3776 weekday, parse :3963-3977 (am_pm = flg&32, x100 = flg&8, screen type = flg2&7, weekday checked = (flg3&128)==0, ddmm = flg3&64 for id 12), write :4129-4142 (same bits).
