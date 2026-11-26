"""Assertions on translated exceptions: the key and placeholders are the behaviour, the wording lives in strings.json."""

from contextlib import contextmanager

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.pvvx_ota.const import DOMAIN


@contextmanager
def raises_key(key, exc=HomeAssistantError, **placeholders):
    with pytest.raises(exc) as info:
        yield info
    err = info.value
    assert err.translation_domain == DOMAIN and err.translation_key == key, (err.translation_key, str(err))
    for name, value in placeholders.items():
        assert err.translation_placeholders[name] == str(value), (name, err.translation_placeholders)
