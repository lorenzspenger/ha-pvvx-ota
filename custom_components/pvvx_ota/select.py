"""Select entities (options are stable ids, the texts come from the translations): TX power, temperature unit, smiley, display content, advertising type."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import config as cfgmod
from .entity import PvvxConfigEntity, wanted
from .shared import PvvxConfigEntry

PARALLEL_UPDATES = 1  # one Bluetooth operation at a time, like the shared lock


async def async_setup_entry(hass: HomeAssistant, entry: PvvxConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    store = entry.runtime_data
    ents: list[PvvxConfigEntity] = []
    if wanted(store, "rf_tx_power"):
        ents.append(TxPowerSelect(hass, entry))
    if wanted(store, "temp_f"):
        ents.append(UnitSelect(hass, entry))
    if wanted(store, "smiley"):
        ents.append(SmileySelect(hass, entry))
    if wanted(store, "screen_type"):
        ents.append(ScreenTypeSelect(hass, entry))
    if wanted(store, "adv_type"):
        ents.append(AdvTypeSelect(hass, entry))
    add(ents)


class TxPowerSelect(PvvxConfigEntity, SelectEntity):
    field_key = "rf_tx_power"

    def __init__(self, hass: HomeAssistant, entry: PvvxConfigEntry) -> None:
        super().__init__(hass, entry, "rf_tx_power", enabled_default=False)

    def _current_id(self) -> str | None:
        v = self.store.value("rf_tx_power")
        if v is None:
            return None
        return cfgmod.tx_power_id(v) if v in cfgmod.TX_POWER else cfgmod.raw_id(v)  # keep a value we do not list

    @property
    def options(self) -> list[str]:
        opts = [cfgmod.tx_power_id(r) for r in sorted(cfgmod.TX_POWER, key=lambda r: cfgmod.TX_POWER[r])]
        cur = self._current_id()
        return opts + [cur] if cur and cur not in opts else opts

    @property
    def current_option(self) -> str | None:
        return self._current_id()

    async def async_select_option(self, option: str) -> None:
        by_id = {cfgmod.tx_power_id(r): r for r in cfgmod.TX_POWER}
        if option not in by_id:
            return  # a raw_N entry only reports the current value
        await self.store.async_request({"rf_tx_power": by_id[option]})


class UnitSelect(PvvxConfigEntity, SelectEntity):
    field_key = "temp_f"
    _attr_options = cfgmod.TEMP_UNITS

    def __init__(self, hass: HomeAssistant, entry: PvvxConfigEntry) -> None:
        super().__init__(hass, entry, "temp_unit_display", enabled_default=False)

    @property
    def current_option(self) -> str | None:
        v = self.store.value("temp_f")
        return None if v is None else cfgmod.TEMP_UNITS[1 if v else 0]

    async def async_select_option(self, option: str) -> None:
        await self.store.async_request({"temp_f": cfgmod.TEMP_UNITS.index(option)})


class SmileySelect(PvvxConfigEntity, SelectEntity):
    field_key = "smiley"
    _attr_options = cfgmod.SMILEYS

    def __init__(self, hass: HomeAssistant, entry: PvvxConfigEntry) -> None:
        super().__init__(hass, entry, "smiley", enabled_default=False)

    @property
    def current_option(self) -> str | None:
        v = self.store.value("smiley")
        return None if v is None else cfgmod.SMILEYS[v]

    async def async_select_option(self, option: str) -> None:
        await self.store.async_request({"smiley": cfgmod.SMILEYS.index(option)})


class ScreenTypeSelect(PvvxConfigEntity, SelectEntity):
    """What the MJWSD05MMC shows. "external" (5) is only a state the sensor can report: it needs data pushed over
    the external-display command, which this integration never sends, so it is not offered as a choice."""

    field_key = "screen_type"

    def __init__(self, hass: HomeAssistant, entry: PvvxConfigEntry) -> None:
        super().__init__(hass, entry, "screen_type", enabled_default=False)

    def _current_id(self) -> str | None:
        v = self.store.value("screen_type")
        return None if v is None else cfgmod.SCREEN_TYPES[v] if v < len(cfgmod.SCREEN_TYPES) else cfgmod.raw_id(v)

    @property
    def options(self) -> list[str]:
        opts = cfgmod.SCREEN_TYPES[:-1]
        cur = self._current_id()
        return opts + [cur] if cur and cur not in opts else opts

    @property
    def current_option(self) -> str | None:
        return self._current_id()

    async def async_select_option(self, option: str) -> None:
        if option in cfgmod.SCREEN_TYPES[:-1]:
            await self.store.async_request({"screen_type": cfgmod.SCREEN_TYPES.index(option)})


class AdvTypeSelect(PvvxConfigEntity, SelectEntity):
    """Disabled by default. Anything but BTHome stops HA's bthome integration from receiving this sensor's data."""

    field_key = "adv_type"
    _attr_options = cfgmod.ADV_TYPES
    warning = "Anything but BTHome stops Home Assistant's BTHome integration from receiving this sensor's data."

    def __init__(self, hass: HomeAssistant, entry: PvvxConfigEntry) -> None:
        super().__init__(hass, entry, "adv_type", enabled_default=False)

    @property
    def current_option(self) -> str | None:
        v = self.store.value("adv_type")
        return None if v is None else cfgmod.ADV_TYPES[v]

    async def async_select_option(self, option: str) -> None:
        await self.store.async_request({"adv_type": cfgmod.ADV_TYPES.index(option)})
