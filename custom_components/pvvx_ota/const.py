"""Constants for pvvx OTA."""

DOMAIN = "pvvx_ota"
RAW_URL = "https://raw.githubusercontent.com/pvvx/ATC_MiThermometer/master/"
RELEASES_URL = "https://github.com/pvvx/ATC_MiThermometer/releases"
CATALOG_TTL = 6 * 3600  # seconds a firmware.json fetch stays valid
CATALOG_MIN_INTERVAL = 600  # never hit GitHub more often than this to refresh a catalog that is already known
CATALOG_RETRY = (
    60.0,
    300.0,
    900.0,
)  # waits after the 1st, 2nd, 3rd+ failed fetch while there is no catalog at all (HTTP only)
CATALOG_POLL_INTERVAL = (
    12 * 3600
)  # seconds between firmware list / release notes refreshes per sensor (HTTP only, no Bluetooth)
CONF_POLL_INTERVAL = "device_poll_interval"  # entry option: how often the sensor's version is read over Bluetooth
POLL_CHOICES: dict[str, int | None] = {"1d": 86400, "3d": 3 * 86400, "7d": 7 * 86400, "never": None}  # seconds
DEFAULT_POLL_INTERVAL = "3d"
POST_FLASH_QUIET = 30  # seconds after a flash in which no other connection to the sensor is opened (it is rebooting)
POST_FLASH_DELAY = 30  # seconds the sensor gets to boot (a pvvx sensor takes a few seconds) before the first version check connects to it
POST_FLASH_RETRY = 30  # seconds between the attempts of that version check
POST_FLASH_ATTEMPTS = 8  # attempts before the "flash may not have been applied" issue is raised
MAX_DOWNLOAD = 0x30000
FIRST_READ_STAGGER = 4.0  # seconds between the first reads of the entries when HA starts (proxies are shared)
FIRST_READ_RETRY = (300.0, 900.0, 3600.0)  # waits before the retries of a failed first read; then the 12 h poll
CONF_VERSION = "installed_version"
CONF_HW_ID = "hw_id"
CONF_SNAPSHOT = "cfg_snapshot"
CONF_CALIB_KNOWN = (
    "calib_known"  # entry data: the chip type has factory values (offsets are offered); absent until read
)
CONF_BETA = "beta"  # config entry option: offer beta firmware
QUEUE_TIMEOUT = 2 * 3600  # seconds a queued firmware operation waits for the ones before it
MIN_BATTERY_V = 2.5  # volts: below this a flash is refused (unless forced)
CONF_AUTO_CLOCK_SYNC = "auto_clock_sync"  # entry option: set the sensor clock on connections made anyway (default on)
CONF_MIN_BATTERY_V = "min_battery_v"  # option: refuse a flash below this voltage (default MIN_BATTERY_V)
CONF_AUTO_RESTORE_ENCRYPTION = "auto_restore_encryption"  # option: switch encryption back on after a flash (default on)
CONF_PIN_BONDED = "pin_bonded"  # entry data: PIN sensor with a bond stored in the proxy named below (set by the flow)
CONF_BONDED_SCANNER = "bonded_scanner"  # entry data: source (MAC) of the proxy that holds the bond
ENCRYPT_SETTLE = 3.0  # seconds after connect before the first command when pair() cannot start the encryption
ENCRYPT_PAIR_TIMEOUT = 6.0  # cap for pair() on a bonded link (it only encrypts with the stored key)
CONF_PIN = (
    "pin"  # entry data of a PIN sensor: used to pair again when the proxy lost the bond (redacted in diagnostics)
)
FORGET_ACTION = (
    "forget_pvvx"  # ESPHome API action (package v0.23.0-beta.3 and later) that removes a sensor's bond from the proxy
)
PAIR_ACTION = "pair_pvvx"  # ESPHome API action of esphome/pvvx_pin_pairing.yaml on a proxy
PAIR_WAIT = 35.0  # fixed wait after calling it when the proxy has no "pvvx pairer busy" entity (older package): open + hold 20 s + close
PAIR_BUSY_TIMEOUT = 45.0  # seconds to wait for that entity to go OFF again
PAIR_BUSY_APPEAR = 3.0  # seconds to give the entity to turn ON after the call (state updates travel through the API)
PAIR_AFTER_IDLE = 1.0  # seconds between the pairer being idle and Home Assistant connecting
PROBE_TIMEOUT = 10.0  # seconds a bonded link gets to answer the config read that proves the encryption works (a sensor with a slow connection interval needs about 2 s per GATT operation)
PAIR_ATTEMPTS = (
    2  # calls of pair_pvvx when the proxy's pairer could not reach the sensor (it is connectable only part of the time)
)
FLASHER_URL = "https://pvvx.github.io/ATC_MiThermometer/TelinkMiFlasher.html"  # a placeholder in the strings (hassfest: no URLs there)
WEAK_RSSI = -75  # dBm: below this a flash is likely slow (warned about, never refused)
