"""Pairing a PIN sensor through an ESPHome proxy that offers the ``pair_pvvx`` action (docs/pin-sensors.md, esphome/pvvx_pin_pairing.yaml).

Home Assistant cannot enter a passkey itself. The proxy can: its YAML package has a generic ble_client that connects to a
MAC, answers the passkey request with a PIN and keeps the bond in its flash. This module finds such a proxy and calls the
action. The PIN is only passed to that action; it is never logged here."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify

from .const import PAIR_ACTION, PAIR_AFTER_IDLE, PAIR_ATTEMPTS, PAIR_BUSY_APPEAR, PAIR_BUSY_TIMEOUT, PAIR_WAIT

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PairingProxy:
    """A proxy that hears the sensor and offers the pairing action."""

    source: str  # the proxy's MAC as habluetooth names it (same as the bonded_scanner of an entry)
    name: str
    service: str  # esphome.<service>


_MAC_SUFFIX = re.compile(r"\s*\(([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\)\s*$")


def node_name(scanner_name: str) -> str:
    """The ESPHome node name from a scanner name. habluetooth names an ESPHome proxy "<node> (<MAC>)"; the service is
    named after the node alone."""
    return _MAC_SUFFIX.sub("", scanner_name)


def pairing_proxies(hass: HomeAssistant, address: str) -> list[PairingProxy]:
    """Proxies that currently hear the sensor and offer ``pair_pvvx``, strongest signal first."""
    try:
        found = list(bluetooth.async_scanner_devices_by_address(hass, address, connectable=True))
    except Exception:  # noqa: BLE001 - no usable scanner data: nothing to offer
        return []
    found.sort(key=lambda f: f.advertisement.rssi, reverse=True)
    out: list[PairingProxy] = []
    for item in found:
        scanner = item.scanner
        name = getattr(scanner, "name", None)
        source = getattr(scanner, "source", None)
        if not name or not source:
            continue
        node = node_name(str(name))
        service = f"{slugify(node)}_{PAIR_ACTION}"
        if hass.services.has_service("esphome", service):
            out.append(PairingProxy(source=str(source), name=node, service=service))
    return out


class PairConnectError(HomeAssistantError):
    """The proxy's pairer never reached the sensor (it was not connectable): nothing was paired."""


def _node_entity(hass: HomeAssistant, node: str, domain: str, label_end: str) -> str | None:
    """Entity of the ESPHome node ``node`` (its config entry's device name) whose name ends with ``label_end``.

    Found through the node's config entry, not the device registry: the device there carries only the Wi-Fi MAC, while a
    scanner's source is the Bluetooth MAC."""
    wanted = slugify(node)
    ent_reg = er.async_get(hass)
    for entry in hass.config_entries.async_entries("esphome"):
        if slugify(str(entry.data.get("device_name") or entry.title)) != wanted:
            continue
        for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
            label = (entity.name or entity.original_name or "").lower()
            if entity.domain == domain and label.endswith(label_end):
                return entity.entity_id
    return None


def busy_entity(hass: HomeAssistant, node: str) -> str | None:
    """The "pvvx pairer busy" binary sensor of the proxy node, None if its package has none (older package)."""
    return _node_entity(hass, node, "binary_sensor", "pvvx pairer busy")


def result_entity(hass: HomeAssistant, node: str) -> str | None:
    """The "pvvx pair result" text sensor of the proxy node, None if its package has none."""
    return _node_entity(hass, node, "sensor", "pvvx pair result")  # an ESPHome text sensor is a sensor entity


async def _wait_until_idle(hass: HomeAssistant, entity_id: str) -> None:
    """The proxy's single pairer holds the sensor's one connection until it is OFF: wait for the pairing to finish (the entity
    turns ON right after the call, OFF after the pairer disconnected)."""
    await asyncio.sleep(PAIR_BUSY_APPEAR)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PAIR_BUSY_TIMEOUT
    while loop.time() < deadline:
        state = hass.states.get(entity_id)
        if state is None or state.state != "on":
            break
        await asyncio.sleep(0.5)
    else:
        _LOGGER.warning("%s still busy after %.0f s, going on anyway", entity_id, PAIR_BUSY_TIMEOUT)
    await asyncio.sleep(PAIR_AFTER_IDLE)


async def async_pair(
    hass: HomeAssistant, proxy: PairingProxy, address: str, pin: int, wait: float = PAIR_WAIT
) -> str | None:
    """Have the proxy pair with the sensor and wait until it is done. The call returns at once; pairing takes about 15 s and the
    proxy's pairer then disconnects. Home Assistant must not connect to the sensor before that (one connection per sensor, and
    the pairer's disconnect would kill a link Home Assistant reused), so wait for the proxy's "pvvx pairer busy" entity to go
    OFF, or a fixed ``wait`` if the package on the proxy is older and has none. If the pairer never reached the sensor ("pvvx
    pair result" says "no connection": the sensor was not connectable) the call is repeated once, then PairConnectError. Returns
    that result ("passkey answered", "no passkey asked", ...) or None if the proxy does not report one."""
    for attempt in range(PAIR_ATTEMPTS):
        _LOGGER.debug("Asking %s to pair with %s", proxy.name, address)
        try:
            await hass.services.async_call("esphome", proxy.service, {"mac": address, "pin": pin}, blocking=True)
        except HomeAssistantError:
            raise
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"Could not call esphome.{proxy.service}: {type(err).__name__}") from err
        busy = busy_entity(hass, proxy.name)
        if busy is None:
            _LOGGER.debug("%s has no pairer-busy entity, waiting %.0f s", proxy.name, wait)
            await asyncio.sleep(wait)
            return None
        await _wait_until_idle(hass, busy)
        result = result_entity(hass, proxy.name)
        state = None if result is None else hass.states.get(result)
        if state is None or state.state != "no connection":
            return None if state is None else state.state
        _LOGGER.debug(
            "%s could not reach %s for pairing (attempt %d of %d)", proxy.name, address, attempt + 1, PAIR_ATTEMPTS
        )
    raise PairConnectError(
        f"{proxy.name} could not connect to {address} to pair it: pvvx sensors are connectable only part of the time. Try again."
    )


async def async_repair(hass: HomeAssistant, address: str, source: str, pin: int) -> bool:
    """Pair again through the proxy ``source`` (the one that holds the bond) with a stored PIN. False if that proxy does not
    offer the action now or the call fails; True once the pairing was started and given time to finish."""
    proxy = next((p for p in pairing_proxies(hass, address) if p.source.upper() == source.upper()), None)
    if proxy is None:
        return False
    try:
        await async_pair(hass, proxy, address, pin)
    except HomeAssistantError as err:
        _LOGGER.debug("Pairing %s again through %s failed: %s", address, proxy.name, err)
        return False
    return True
