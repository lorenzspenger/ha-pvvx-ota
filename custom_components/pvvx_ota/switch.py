"""Switch entities: display flags (board specific, see config.DISPLAY_BY_HW), PHY flags."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_BETA
from .entity import PvvxConfigEntity, wanted
from .shared import PvvxConfigEntry

# key -> enabled by default. The PHY switches are opt-in: coded-PHY/BT5 adverts may not reach ESP32 proxies,
# the sensor can vanish from HA until it is power cycled (long range resets on power-off).
PARALLEL_UPDATES = 1  # one Bluetooth operation at a time, like the shared lock


SWITCHES = {
    "show_battery": False,
    "comfort_smiley": False,
    "screen_off": False,
    "x100": False,
    "time_am_pm": False,
    "date_ddmm": False,
    "show_weekday": False,
    "longrange": False,
    "bt5phy": False,
}
# entity key -> config field when they differ: the sensor stores "weekday hidden", the switch says "show weekday"
FIELD_OF = {"show_weekday": "not_day_of_week"}
INVERTED = frozenset({"show_weekday"})
WARNINGS = {
    "longrange": "ESP32 proxies may not receive coded-PHY adverts: the sensor can vanish from Home Assistant until it is "
    "power cycled (long range resets on power-off).",
    "bt5phy": "ESP32 proxies may not receive BT5 PHY adverts: the sensor can vanish from Home Assistant.",
}


async def async_setup_entry(hass: HomeAssistant, entry: PvvxConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    store = entry.runtime_data
    add([BetaSwitch(hass, entry)])  # a local option: always there
    add([FlagSwitch(hass, entry, key, on) for key, on in SWITCHES.items() if wanted(store, FIELD_OF.get(key, key))])


class FlagSwitch(PvvxConfigEntity, SwitchEntity):
    field_key: str

    def __init__(self, hass: HomeAssistant, entry: PvvxConfigEntry, key: str, enabled_default: bool) -> None:
        self.field_key = FIELD_OF.get(key, key)
        self.inverted = key in INVERTED
        self.warning = WARNINGS.get(key)
        super().__init__(hass, entry, key, enabled_default)

    @property
    def is_on(self) -> bool | None:
        v = self.store.value(self.field_key)
        return None if v is None else bool(v) != self.inverted

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.store.async_request({self.field_key: 0 if self.inverted else 1})

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.store.async_request({self.field_key: 1 if self.inverted else 0})


class BetaSwitch(PvvxConfigEntity, SwitchEntity):
    """Offer pvvx beta firmware instead of the stable release (when one exists for this hardware).

    Kept in the config entry options. Off by default; a beta is never offered as a downgrade."""

    def __init__(self, hass: HomeAssistant, entry: PvvxConfigEntry) -> None:
        self._entry = entry
        super().__init__(hass, entry, "beta_firmware", enabled_default=False)

    @property
    def available(self) -> bool:
        return True  # a local option, independent of the sensor connection

    @property
    def is_on(self) -> bool:
        return bool(self._entry.options.get(CONF_BETA, False))

    async def _set(self, on: bool) -> None:
        self.hass.config_entries.async_update_entry(self._entry, options={**self._entry.options, CONF_BETA: on})
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
