"""Base class for the configuration entities (all on the same device as the update entity)."""

from __future__ import annotations

from homeassistant.const import CONF_ADDRESS, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import Entity

from .const import CONF_VERSION
from .ota import fmt_version
from .registry import attach_to_device
from .shared import PvvxConfigEntry
from .store import ConfigStore


class PvvxConfigEntity(Entity):
    """Config-category entity bound to a ConfigStore. The update entity reloads the entry when the device changes."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False
    field_key: str | None = None  # None: not tied to one config field (buttons)
    warning: str | None = None  # shown as an attribute: what can go wrong (kept out of the name / entity id)

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        return {"warning": self.warning} if self.warning else None

    def __init__(self, hass: HomeAssistant, entry: PvvxConfigEntry, key: str, enabled_default: bool = True) -> None:
        self._attr_entity_registry_enabled_default = enabled_default  # dangerous ones are opt-in
        self.store = entry.runtime_data
        address: str = entry.data[CONF_ADDRESS]
        self._attr_unique_id = f"{address}_{key}"
        self._attr_translation_key = key  # the name comes from strings.json / translations (English, German)
        attach_to_device(self, hass, address, entry.entry_id, entry.title, fmt_version(entry.data.get(CONF_VERSION)))

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.store.async_add_listener(self._store_changed))

    @callback
    def _store_changed(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self.store.available and (self.field_key is None or self.field_key in self.store.capabilities)


def wanted(store: ConfigStore, *keys: str) -> bool:
    """Whether to create these entities: decided from the version/board the entry knows (stored by the config flow,
    which only creates an entry after an authenticated read). If a later read fails, the entities stay but are
    unavailable until a read works again (Refresh button)."""
    return set(keys) <= store.stored_capabilities()
