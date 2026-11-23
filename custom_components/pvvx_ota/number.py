"""Number entities: advertising interval, measure interval."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import calib as calibmod
from .const import CONF_CALIB_KNOWN, CONF_VERSION, DOMAIN
from .entity import PvvxConfigEntity, get_store, wanted

PARALLEL_UPDATES = 1  # one Bluetooth operation at a time, like the shared lock


ADV_UNIT_S = 0.0625  # the sensor counts in 62.5 ms


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    store = get_store(hass, entry)
    ents: list[PvvxConfigEntity] = []
    if wanted(store, "adv_interval"):
        ents.append(AdvIntervalNumber(hass, entry))
    if wanted(store, "measure_interval"):
        ents.append(MeasureIntervalNumber(hass, entry))
    # The chip type is only known after the (background) first read, so the offsets are created for every verified sensor
    # until a read says the chip has no factory values; then they are not offered (and removed from the registry).
    has_coef = (entry.data.get(CONF_VERSION) or 0) >= calibmod.MIN_VERSION  # command 0x25 exists from 4.8
    if wanted(store, "adv_interval") and has_coef and entry.data.get(CONF_CALIB_KNOWN, True):
        ents += [OffsetNumber(hass, entry, "temp_offset", "°C"), OffsetNumber(hass, entry, "humi_offset", "%")]
    elif entry.data.get(CONF_CALIB_KNOWN, True) is False:
        ent_reg = er.async_get(hass)
        for key in ("temp_offset", "humi_offset"):
            if entity_id := ent_reg.async_get_entity_id("number", DOMAIN, f"{entry.data[CONF_ADDRESS]}_{key}"):
                ent_reg.async_remove(entity_id)
    add(ents)


class AdvIntervalNumber(PvvxConfigEntity, NumberEntity):
    field_key = "adv_interval"
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_native_min_value = ADV_UNIT_S
    _attr_native_max_value = 160 * ADV_UNIT_S
    _attr_native_step = ADV_UNIT_S

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, "adv_interval", "Advertising interval", enabled_default=False)

    @property
    def native_value(self) -> float | None:
        v = self.store.value("adv_interval")
        return None if v is None else v * ADV_UNIT_S

    async def async_set_native_value(self, value: float) -> None:
        await self.store.async_request({"adv_interval": round(value / ADV_UNIT_S)})


class MeasureIntervalNumber(PvvxConfigEntity, NumberEntity):
    """Measurement every N advertising intervals."""

    warning = "Counted in advertising intervals: a measurement is taken every N advertising intervals."

    field_key = "measure_interval"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 2
    _attr_native_max_value = 25
    _attr_native_step = 1

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, "measure_interval", "Measure interval", enabled_default=False)

    @property
    def native_value(self) -> float | None:
        return self.store.value("measure_interval")

    async def async_set_native_value(self, value: float) -> None:
        await self.store.async_request({"measure_interval": int(value)})


class OffsetNumber(PvvxConfigEntity, NumberEntity):
    """Calibration offset of the temperature/humidity reading (sensor coefficient Tz/Hz, see calib.py). Disabled by default."""

    _attr_mode = NumberMode.BOX
    _attr_native_min_value = -12.5
    _attr_native_max_value = 12.5
    _attr_native_step = 0.1
    warning = (
        "Shifts the measured value by this amount, stored in the sensor (relative to the chip's factory value). "
        "Slope factors are not touched. Only change it if you compared the sensor with a reference."
    )

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, key: str, unit: str) -> None:
        self._key = key
        self._attr_native_unit_of_measurement = unit
        super().__init__(hass, entry, key, key, enabled_default=False)

    @property
    def available(self) -> bool:
        calib = self.store.calib
        return self.store.available and calib is not None and calib.known

    @property
    def native_value(self) -> float | None:
        calib = self.store.calib
        return None if calib is None else getattr(calib, self._key)

    async def async_set_native_value(self, value: float) -> None:
        await self.store.async_set_calibration(**{self._key: value})
