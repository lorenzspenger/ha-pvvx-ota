"""The private habluetooth attributes the integration relies on exist in the installed version.

Everything else in the suite uses fakes for these (tests/test_pin_bond.py), so a Home Assistant or habluetooth release that
renames them would pass CI and silently downgrade PIN support at runtime. This file is the one that fails first, and says
which attribute went. Names come from bt_internals so there is a single list.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest
from bleak.backends.device import BLEDevice
from habluetooth import wrappers
from habluetooth.usage import HaBleakClientWithServiceCache

from custom_components.pvvx_ota import bt_internals


def _hint(name: str) -> str:
    return (
        f"habluetooth no longer provides {name!r}: custom_components/pvvx_ota/bt_internals.py (PIN connection pinning) "
        "has to be adapted to the new habluetooth before this combination is supported."
    )


@pytest.mark.parametrize("name", [bt_internals._CHOOSER, bt_internals._BACKEND_FOR])
def test_installed_client_class_has_the_hook_methods(name):
    # Home Assistant swaps bleak_retry_connector.BleakClientWithServiceCache for this class (habluetooth.usage).
    assert callable(getattr(HaBleakClientWithServiceCache, name, None)), _hint(name)


def test_hook_signatures_still_fit_the_subclass_override():
    chooser = inspect.signature(getattr(HaBleakClientWithServiceCache, bt_internals._CHOOSER))
    assert list(chooser.parameters) == ["self", "manager"], _hint(bt_internals._CHOOSER) + f" Signature: {chooser}"
    backend = inspect.signature(getattr(HaBleakClientWithServiceCache, bt_internals._BACKEND_FOR))
    assert list(backend.parameters) == ["self", "manager", "scanner", "ble_device"], (
        _hint(bt_internals._BACKEND_FOR) + f" Signature: {backend}"
    )


def test_connected_scanner_is_an_instance_attribute_of_a_new_client(monkeypatch):
    monkeypatch.setattr(wrappers, "get_manager", MagicMock())
    client = HaBleakClientWithServiceCache(BLEDevice("AA:BB:CC:DD:EE:FF", "x", {}))
    assert hasattr(client, bt_internals._CONNECTED_SCANNER), _hint(bt_internals._CONNECTED_SCANNER)
    assert bt_internals.connected_scanner(client) is None  # not connected yet


def test_set_connection_params_exists_for_the_fast_link_request():
    # ota.request_fast_link tolerates a missing method; this only tells us the faster link silently stopped working.
    assert callable(getattr(HaBleakClientWithServiceCache, "set_connection_params", None)), _hint(
        "set_connection_params"
    )
