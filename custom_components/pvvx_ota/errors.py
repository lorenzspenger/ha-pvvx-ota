"""Translated exceptions: every user-facing error is a translation key under ``exceptions`` in strings.json."""

from __future__ import annotations

from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError, ServiceValidationError

from .const import DOMAIN


def _placeholders(values: dict[str, object]) -> dict[str, str]:
    return {key: str(value) for key, value in values.items()}


def ha_error(key: str, **placeholders: object) -> HomeAssistantError:
    """A failure of the operation (the sensor, the link, a proxy)."""
    return HomeAssistantError(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=_placeholders(placeholders)
    )


def validation_error(key: str, **placeholders: object) -> ServiceValidationError:
    """Input the caller can fix (a bad value, a path that is not allowed, a missing confirmation)."""
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=_placeholders(placeholders)
    )


def not_ready(key: str, **placeholders: object) -> ConfigEntryNotReady:
    """The entry cannot be set up yet; Home Assistant retries."""
    return ConfigEntryNotReady(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=_placeholders(placeholders)
    )
