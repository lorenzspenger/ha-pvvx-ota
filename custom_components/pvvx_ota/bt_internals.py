"""The only module that touches private habluetooth/bleak attributes.

Home Assistant offers no supported way to say "connect through this proxy" or to ask which proxy a connection really runs
through. A PIN sensor only answers through the proxy that holds its bond, so this integration needs both. Everything private
is here, guarded, and degrades to the normal connection when a Home Assistant release changes it:

* ``BleakClientWithServiceCache._async_get_best_available_backend_and_device`` (habluetooth's connect wrapper chooses the
  best-scored path and ignores the BLEDevice it is given): replaced in a subclass by "the paths of this scanner only".
  Missing: ``pinned_client_class`` logs once at WARNING and returns None; the caller connects normally and the check after
  connecting (``connected_scanner``) is the safety net.
* ``client._connected_scanner`` (the scanner the link really runs through). Missing: ``connected_scanner`` returns None; a
  PIN-pinned connection whose path cannot be verified is refused by the caller, nothing is sent.

tests/test_pin_bond.py simulates both attributes being missing.
"""

from __future__ import annotations

import logging
from typing import Any

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache

_LOGGER = logging.getLogger(__name__)

_CHOOSER = "_async_get_best_available_backend_and_device"  # habluetooth's wrapper: the path a connection takes
_BACKEND_FOR = "_async_get_backend_for_ble_device"  # habluetooth's wrapper: a backend for one scanner's path
_CONNECTED_SCANNER = "_connected_scanner"  # habluetooth's wrapper: the scanner of the live connection
_warned = False


def pinned_client_class(source: str, address: str) -> type[BleakClientWithServiceCache] | None:
    """A client class that connects through the scanner ``source`` only, None if this habluetooth has no such hook."""
    global _warned  # noqa: PLW0603 - log once per run, not on every connection
    parent: Any = BleakClientWithServiceCache
    if not hasattr(parent, _CHOOSER) or not hasattr(parent, _BACKEND_FOR):
        if not _warned:
            _warned = True
            _LOGGER.warning(
                "This Home Assistant version no longer has the Bluetooth hook for connecting through one specific proxy; "
                "connections use the normal proxy choice. PIN sensors are checked after connecting and refused when the "
                "path cannot be verified."
            )
        return None

    class PinnedClient(parent):  # type: ignore[misc]
        def _async_get_best_available_backend_and_device(self, manager: Any) -> Any:
            for found in sorted(
                manager.async_scanner_devices_by_address(address, True),
                key=lambda f: f.advertisement.rssi,
                reverse=True,
            ):
                if getattr(found.scanner, "source", None) != source:
                    continue
                backend = getattr(self, _BACKEND_FOR)(manager, found.scanner, found.ble_device)
                if backend:
                    return backend
            raise BleakError(f"the paired proxy {source} has no free connection to {address} right now")

    return PinnedClient


def connected_scanner(client: Any) -> Any:
    """The scanner (proxy/adapter) the connection really runs through, None if habluetooth does not say."""
    return getattr(client, _CONNECTED_SCANNER, None)
