"""Connection helper: HA's Bluetooth stack picks the adapter or proxy."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from habluetooth import BluetoothReachabilityIntent, BluetoothScannerDevice
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .bt_internals import connected_scanner, pinned_client_class
from .const import (
    CONF_ADDRESS,
    CONF_BONDED_SCANNER,
    CONF_PIN,
    CONF_PIN_BONDED,
    DOMAIN,
    ENCRYPT_PAIR_TIMEOUT,
    ENCRYPT_SETTLE,
    PROBE_TIMEOUT,
)
from .errors import ha_error
from .ota import NotPvvxError, OtaError, PairingRequiredError, read_cfg_raw
from .pairing import async_repair, node_name
from .shared import get_shared

DISCONNECT_TIMEOUT = (
    8.0  # a proxy that never confirms the disconnect must not hold things up (or the link open) silently
)
_LOGGER = logging.getLogger(__name__)
CONNECT_TIMEOUT = 25.0  # seconds for the whole connect (all retries, all proxies); the flow must not hang for minutes


def _why_unreachable(hass: HomeAssistant, address: str) -> str:
    try:
        return bluetooth.async_address_reachability_diagnostics(hass, address, BluetoothReachabilityIntent.CONNECTION)
    except Exception:  # noqa: BLE001 - diagnostics are best effort
        return "no connectable adapter or proxy has seen it recently"


def scanner_device(hass: HomeAssistant, address: str, source: str) -> BluetoothScannerDevice | None:
    """The path through the scanner ``source`` (BluetoothScannerDevice), None if that proxy does not see the sensor now."""
    try:
        found = bluetooth.async_scanner_devices_by_address(hass, address, connectable=True)
    except Exception:  # noqa: BLE001 - no scanner data: treat as not seen
        return None
    return next((f for f in found if str(getattr(f.scanner, "source", "")).upper() == source.upper()), None)


def entry_for(hass: HomeAssistant, address: str) -> ConfigEntry | None:
    """The config entry of this sensor, None if there is none (e.g. while the config flow still verifies it)."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if str(entry.data.get(CONF_ADDRESS, "")).upper() == address.upper():
            return entry
    return None


def expected_path(hass: HomeAssistant, address: str) -> tuple[str, int] | None:
    """(proxy name, advert RSSI) of the path a connection to this sensor will use: the paired proxy for a PIN-bonded sensor,
    else the strongest one (habluetooth also weighs free slots and failures, so this is the likely path, not a promise)."""
    try:
        found = list(bluetooth.async_scanner_devices_by_address(hass, address, connectable=True))
    except Exception:  # noqa: BLE001 - no scanner data
        return None
    bonded, source, _pin = bond_for(hass, address)
    if bonded and source:
        found = [f for f in found if str(getattr(f.scanner, "source", "")).upper() == source.upper()]
    if not found:
        return None
    best = max(found, key=lambda f: f.advertisement.rssi)
    return node_name(str(getattr(best.scanner, "name", "") or best.scanner.source)), int(best.advertisement.rssi)


def bond_for(hass: HomeAssistant, address: str) -> tuple[bool, str | None, int | None]:
    """(pin_bonded, source of the proxy holding the bond, stored PIN) from the entry of this sensor; (False, None, None)
    for any other."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if str(entry.data.get(CONF_ADDRESS, "")).upper() == address.upper():
            pin = entry.data.get(CONF_PIN)
            return (
                bool(entry.data.get(CONF_PIN_BONDED)),
                entry.data.get(CONF_BONDED_SCANNER),
                pin if isinstance(pin, int) else None,
            )
    return False, None, None


def connected_source(client: Any) -> str | None:
    """Source (MAC) of the proxy/adapter the connection really runs through, None if habluetooth does not say."""
    return getattr(connected_scanner(client), "source", None)


async def encrypt_link(client: Any, name: str, t0: float) -> None:
    """Make the link encrypted with the stored bond before the first command.

    A proxy does not start encryption by itself and drops the link after a refused write, so nothing may be sent before.
    pair() on a bonded device only encrypts with the stored key; if it is missing or fails, give pvvx's own Security
    Request (about 1 s after connect, encryption done after about 2 s) ENCRYPT_SETTLE seconds. The extra time is capped."""
    pair = getattr(client, "pair", None)
    if pair is None:
        _LOGGER.debug("%s: no pair() on this client, waiting for the sensor's Security Request", name)
    else:
        try:
            async with asyncio.timeout(ENCRYPT_PAIR_TIMEOUT):
                await pair()
            _LOGGER.debug("%s: link encrypted by pair() %.1f s after connect", name, time.monotonic() - t0)
            return
        except Exception as err:  # noqa: BLE001 - fall back to waiting for the sensor's Security Request
            _LOGGER.debug(
                "%s: pair() did not encrypt the link after %.1f s (%s%s), waiting for the sensor's Security Request",
                name,
                time.monotonic() - t0,
                type(err).__name__,
                f": {err}" if str(err) else "",
            )
    remaining = ENCRYPT_SETTLE - (time.monotonic() - t0)
    if remaining > 0:
        await asyncio.sleep(remaining)
    _LOGGER.debug("%s: first command sent %.1f s after connect", name, time.monotonic() - t0)


async def _open(
    hass: HomeAssistant,
    address: str,
    name: str,
    *,
    encrypt: bool | None = None,
    bonded_scanner: str | None = None,
) -> BleakClientWithServiceCache:
    """Connect (via the best proxy/adapter); the caller disconnects.

    For a PIN sensor with a bond (``encrypt``; looked up from the entry unless given) the connection must run through the
    proxy that holds the bond (``bonded_scanner``): another one is refused anyway and must not send anything to the
    sensor, so the link is dropped with a clear error. Then the link is encrypted before the first command. Sensors
    without a bond: nothing changes."""
    pinned = None
    if encrypt and bonded_scanner:
        # a PIN sensor only answers through the proxy that holds its bond: choose that path now, before connecting
        pinned = scanner_device(hass, address, bonded_scanner)
        if pinned is None:
            raise ha_error("paired_proxy_does_not_see_sensor", name=name, proxy=bonded_scanner)
    device = (
        pinned.ble_device
        if pinned is not None
        else bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    )
    if device is None:
        raise ha_error("not_connectable", name=name, address=address, reason=_why_unreachable(hass, address))

    def _refresh() -> BLEDevice:
        if pinned is not None:
            again = scanner_device(hass, address, bonded_scanner or "")
            return again.ble_device if again is not None else device
        return bluetooth.async_ble_device_from_address(hass, address, connectable=True) or device

    client_class: type[BleakClientWithServiceCache] = BleakClientWithServiceCache
    if pinned is not None and bonded_scanner:
        client_class = pinned_client_class(bonded_scanner, address) or BleakClientWithServiceCache
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            client = await establish_connection(client_class, device, name, ble_device_callback=_refresh)
    except TimeoutError as err:
        # not a PIN (a PIN sensor answers within seconds): the connection itself never completed
        raise ha_error(
            "connect_timeout",
            name=name,
            address=address,
            seconds=f"{CONNECT_TIMEOUT:.0f}",
            reason=_why_unreachable(hass, address),
        ) from err
    except Exception as err:
        raise ha_error("connect_failed", name=name, address=address, error=err) from err
    link = describe_link(hass, client, address)
    _LOGGER.debug("Connected to %s (%s) via %s", name, address, link)

    get_shared(hass).links[address] = link
    t0 = time.monotonic()  # the settle time below counts from the connection being up, not from the connect attempt
    try:
        if encrypt:
            source = connected_source(client)
            if bonded_scanner and source is None:
                # the path cannot be verified (and may not have been pinned): nothing is sent to a PIN sensor over it
                raise ha_error("proxy_not_verifiable", name=name, proxy=bonded_scanner)
            if bonded_scanner and source and source.upper() != bonded_scanner.upper():
                raise ha_error("connected_via_wrong_proxy", name=name, proxy=bonded_scanner, link=link)
            await encrypt_link(client, name, t0)
    except BaseException:
        await disconnect_quietly(client, name)
        raise
    return client


async def _verify_link(client: Any, name: str) -> None:
    """A bonded link must answer the config command (a cheap read) before anything else is sent over it: a bond the sensor
    no longer knows gives a refusal (error 5/15) or a link that never replies."""
    try:
        await read_cfg_raw(client, PROBE_TIMEOUT)
    except NotPvvxError as silent:
        raise PairingRequiredError(f"{name}: no reply on the encrypted link (the bond is not accepted)") from silent


@asynccontextmanager
async def async_connected(
    hass: HomeAssistant,
    address: str,
    name: str,
    *,
    encrypt: bool | None = None,
    bonded_scanner: str | None = None,
) -> AsyncIterator[BleakClientWithServiceCache]:
    """Connect (via the best proxy/adapter) and always disconnect again.

    For a PIN sensor with a bond (``encrypt``; looked up from the entry unless given) the connection must run through the
    proxy that holds the bond (``bonded_scanner``): another one is refused anyway and must not send anything to the
    sensor, so the link is dropped with a clear error. Then the link is encrypted and checked with a config read before
    the caller gets it. If the check fails (the proxy or the sensor lost the bond) the sensor is paired again through
    that proxy with the PIN stored in its entry, once; without a PIN or when that does not help PairingRequiredError is
    raised. Sensors without a bond: nothing changes."""
    pin: int | None = None
    from_entry = encrypt is None  # an operation on a configured sensor, not the config flow's own verification
    if encrypt is None:
        encrypt, bonded_scanner, pin = bond_for(hass, address)
    elif encrypt:
        pin = bond_for(hass, address)[2]
    client = await _open(hass, address, name, encrypt=encrypt, bonded_scanner=bonded_scanner)
    if encrypt:
        try:
            await _verify_link(client, name)
        except (PairingRequiredError, OtaError) as first:
            await disconnect_quietly(client, name)
            if not (pin and bonded_scanner and await async_repair(hass, address, bonded_scanner, pin)):
                if from_entry and not pin and (entry := entry_for(hass, address)) is not None:
                    # a dead bond and no PIN to pair again with: ask for it (Home Assistant starts at most one reauth
                    # flow per entry). Not for mere unreachability: that never gets here (no connection, no verification).
                    entry.async_start_reauth(hass)
                raise PairingRequiredError(
                    f"{name}: the bond with the paired proxy does not work any more and could not be restored "
                    f"({'no PIN stored' if not pin else 'pairing again failed'}): {first}"
                ) from first
            _LOGGER.info("%s: bond restored by pairing again through the paired proxy", name)
            client = await _open(hass, address, name, encrypt=True, bonded_scanner=bonded_scanner)
            try:
                await _verify_link(client, name)
            except BaseException:
                await disconnect_quietly(client, name)
                raise
        except BaseException:
            await disconnect_quietly(client, name)
            raise
    try:
        yield client
    finally:
        await disconnect_quietly(client, name)


async def disconnect_quietly(client: Any, name: str) -> None:
    """Drop the link now, bounded and logged: an open link keeps a rebooting sensor from advertising and the proxy slot busy."""
    try:
        async with asyncio.timeout(DISCONNECT_TIMEOUT):
            await client.disconnect()
        _LOGGER.debug("%s: disconnected", name)
    except TimeoutError:
        _LOGGER.warning("%s: the proxy did not confirm the disconnect within %.0f s", name, DISCONNECT_TIMEOUT)
    except Exception as err:  # noqa: BLE001 - may already be gone after the reboot
        _LOGGER.debug("%s: disconnect ended with %s: %s", name, type(err).__name__, err)


def describe_link(hass: HomeAssistant, client: Any, address: str) -> str:
    """Name, source MAC and RSSI of the proxy/adapter the connection really runs through (not just the one that last
    heard an advertisement). habluetooth keeps the chosen path in a private attribute; if it is not there, say so."""
    scanner = connected_scanner(client)
    if scanner is None:
        return "an unknown proxy (this habluetooth version does not expose the connected scanner)"
    source = getattr(scanner, "source", None)
    name = getattr(scanner, "name", None) or source
    rssi = None
    try:
        for found in bluetooth.async_scanner_devices_by_address(hass, address, connectable=True):
            if getattr(found.scanner, "source", None) == source:
                rssi = found.advertisement.rssi
    except Exception:  # noqa: BLE001 - RSSI is a nicety
        pass
    label = str(name)
    if source and source.upper() not in label.upper():  # habluetooth names ESPHome proxies "<node> (<MAC>)" already
        label = f"{label} ({source})"
    return f"{label}, RSSI {rssi if rssi is not None else 'unknown'}"
