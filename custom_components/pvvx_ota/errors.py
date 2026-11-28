"""Translated exceptions: every user-facing error is a translation key under ``exceptions`` in strings.json."""

from __future__ import annotations

from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError, ServiceValidationError

from .const import DOMAIN
from .ota import NotPvvxError, OtaLinkLost, OtaSlowLink, OtaTimeout


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


def ota_error(name: str, err: Exception) -> HomeAssistantError:
    """A failed Bluetooth operation. The common classes get a text of their own (so the whole message is translated);
    anything else keeps the sensor's own wording after a translated prefix."""
    if isinstance(err, OtaTimeout):
        return ha_error("sensor_timeout", name=name)
    if isinstance(err, OtaLinkLost):
        return ha_error("link_lost", name=name)
    if isinstance(err, OtaSlowLink):
        return ha_error("slow_link", name=name)
    if isinstance(err, NotPvvxError):
        return ha_error("not_pvvx", name=name)
    return ha_error("sensor_error", name=name, error=err)
