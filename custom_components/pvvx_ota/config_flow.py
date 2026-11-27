"""Config flow: one entry per sensor."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import SOURCE_IGNORE, ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import selector

from .const import (
    CONF_AUTO_CLOCK_SYNC,
    CONF_AUTO_RESTORE_ENCRYPTION,
    CONF_BONDED_SCANNER,
    CONF_HW_ID,
    CONF_MIN_BATTERY_V,
    CONF_PIN,
    CONF_PIN_BONDED,
    CONF_POLL_INTERVAL,
    CONF_VERSION,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    FLASHER_URL,
    MIN_BATTERY_V,
    POLL_CHOICES,
)
from .device import async_connected, connected_source
from .ota import NotPvvxError, OtaError, PairingRequiredError, read_cfg
from .pairing import PairConnectError, async_pair, pairing_proxies
from .registry import find_devices
from .shared import get_shared

_LOGGER = logging.getLogger(__name__)

# pvvx custom firmware names itself <MODEL>_<last 3 MAC bytes>, e.g. ATC_F02AED
NAME_RE = re.compile(r"^[A-Za-z0-9]+_[0-9A-F]{6}$")
SERVICE_181A = "0000181a-0000-1000-8000-00805f9b34fb"
SERVICE_FCD2 = "0000fcd2-0000-1000-8000-00805f9b34fb"


# Telink/Xiaomi OUI the stock sensors use. A hint only: BTHome is shared with other vendors.
MAC_PREFIXES = ("A4:C1:38",)


def looks_like_pvvx(info: BluetoothServiceInfoBleak) -> bool:
    """Cheap, advertisement-only hint. Never proves anything: the confirm step connects to verify.

    Passive proxies never see the scan response, so the name may be missing; encrypted BTHome
    hides the payload. Hence name OR the custom/ATC 0x181A format OR the Telink MAC prefix.
    """
    return (
        bool(NAME_RE.match(info.name or ""))
        or SERVICE_181A in info.service_data
        or info.address.upper().startswith(MAC_PREFIXES)
    )


def candidate(info: BluetoothServiceInfoBleak) -> bool:
    """Anything that could be a pvvx sensor (shown in the manual picker)."""
    return looks_like_pvvx(info) or SERVICE_FCD2 in info.service_data


def display_name(hass: HomeAssistant, info: BluetoothServiceInfoBleak) -> str:
    """ "<name> (<last 3 MAC bytes>)" for everything the user sees.

    Name: the BTHome device in the device registry (user-given name first), else the advertised
    pvvx name, else just the MAC. The MAC tail keeps same-named sensors distinguishable.
    """
    address = info.address.upper()
    device = next(iter(find_devices(hass, address)), None)
    name = (device and (device.name_by_user or device.name)) or (info.name if NAME_RE.match(info.name or "") else "")
    return f"{name} ({address[-8:]})" if name else address


def _schema(fields: dict[Any, Any]) -> Any:
    """A voluptuous schema, typed loosely: Home Assistant's own annotations name probatio's classes, which it still accepts here."""
    return vol.Schema(fields)


@dataclass
class PairOutcome:
    """Result of pairing a PIN sensor through its proxy."""

    ok: tuple[int | None, int | None, str] | None = None  # (version, hardware id, proxy source)
    error: str | None = None  # form error key of the last failure
    abort: str | None = None  # flow abort reason (not a pvvx sensor)


class PvvxOtaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options: battery threshold, automatic clock sync, automatic encryption restore."""
        return PvvxOptionsFlow()

    def __init__(self) -> None:
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._found: dict[str, BluetoothServiceInfoBleak] = {}
        self._error_detail = ""
        self._pair_failures = 0
        self._pair_task: asyncio.Task[PairOutcome] | None = None  # the running pairing (progress step)
        self._pair_pin = 0  # the PIN of that pairing, kept for the entry; never logged
        self._pin_target: tuple[str, str] | None = None  # (address, name) of the sensor the PIN step is for

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        """Discovered through the manifest matchers."""
        if not looks_like_pvvx(discovery_info):
            return self.async_abort(reason="not_pvvx")
        await self.async_set_unique_id(discovery_info.address.upper())
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": display_name(self.hass, discovery_info)}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Connect once to prove this is a pvvx device, then create the entry."""
        assert self._discovery is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._async_verify_and_create(self._discovery, errors, discovered=True)
            if result is not None:
                return result
        else:
            self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": display_name(self.hass, self._discovery), "error": self._error_detail},
            errors=errors,
        )

    async def _async_verify_and_create(
        self, info: BluetoothServiceInfoBleak, errors: dict[str, str], discovered: bool = False
    ) -> ConfigFlowResult | None:
        address = info.address.upper()
        name = display_name(self.hass, info)
        bonded_source: str | None = None
        source: str | None = None
        try:
            try:
                async with get_shared(self.hass).lock, async_connected(self.hass, address, name) as client:
                    source = connected_source(client)
                    ver, hw = await read_cfg(client)
            except PairingRequiredError as err:
                _LOGGER.debug("Pairing-like error while verifying %s: %s", address, err, exc_info=err)
                if source is None:
                    raise
                if pairing_proxies(self.hass, address):
                    # a proxy offers the pairing action: ask for the PIN and pair through it. No bond probing first: on a
                    # proxy without a bond pair() stalls on the passkey request and leaves its security manager busy
                    # until it is rebooted (seen on hardware), which breaks the pairing that follows.
                    self._pin_target = (address, name)
                    return await self.async_step_pin()
                # no pairing proxy: the sensor may have a bond from a hand-written ble_client in the proxy that served this
                # connection: encrypt with it and read again through that proxy only
                try:
                    async with (
                        get_shared(self.hass).lock,
                        async_connected(self.hass, address, name, encrypt=True, bonded_scanner=source) as client,
                    ):
                        ver, hw = await read_cfg(client)
                except NotPvvxError as silent:
                    # without a bond the proxy's pair() stalls on the passkey request (nobody answers it) and the
                    # sensor then never replies: that is "no bond", not "not a pvvx sensor" (it just refused with 15)
                    raise PairingRequiredError("no bond: the sensor stayed silent after pair()") from silent
                bonded_source = source
                _LOGGER.info("%s has a PIN and a bond in %s: added as a PIN-bonded sensor", address, source)
        except NotPvvxError:
            return self.async_abort(reason="not_pvvx")
        except PairingRequiredError as err:
            _LOGGER.debug("PIN sensor %s has no usable bond via %s: %s", address, source, err)
            if not discovered:
                # the user picked it by hand: tell them why it cannot be added, change nothing else
                return self.async_abort(reason="pin_protected_manual")
            # found by discovery and nobody can pair it: remember it as ignored (what the "Ignore" button does) so discovery
            # stops offering it; after setting up pairing on a proxy (docs/pin-sensors.md) it can be un-ignored on the integration page
            await self.hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IGNORE}, data={"unique_id": address, "title": name}
            )
            return self.async_abort(reason="pin_protected")
        except (HomeAssistantError, OtaError) as err:
            _LOGGER.warning("Could not verify %s: %s", address, err)
            errors["base"] = "cannot_connect"
            return None
        except Exception as err:  # noqa: BLE001 - show the real cause instead of "Unknown error"
            _LOGGER.exception("Unexpected error while verifying %s", address)
            errors["base"] = "unexpected"
            self._error_detail = f"{type(err).__name__}: {err}"
            return None
        if ver is None:
            return self.async_abort(reason="not_pvvx")
        data = {CONF_ADDRESS: address, CONF_VERSION: ver, CONF_HW_ID: hw}
        if bonded_source is not None:
            data |= {CONF_PIN_BONDED: True, CONF_BONDED_SCANNER: bonded_source}
        return self.async_create_entry(title=name, data=data)

    # -- PIN pairing: one progress step per flow (new sensor, reauth, reconfigure) --------------------------------
    #
    # Pairing and verifying takes about 30 to 40 s (twice that if the pairer has to be asked again), so the work runs as a task
    # behind a progress step: the form's submit starts it, Home Assistant shows "pairing", and <step>_done
    # reads the outcome (entry, error and form, or abort).

    def _pin_form(self, step_id: str, name: str, errors: dict[str, str] | None = None) -> ConfigFlowResult:
        return self.async_show_form(
            step_id=step_id,
            data_schema=_schema(
                {
                    vol.Required(CONF_PIN): selector.NumberSelector(
                        selector.NumberSelectorConfig(min=1, max=999999, step=1, mode=selector.NumberSelectorMode.BOX)
                    )
                }
            ),
            description_placeholders={"name": name, "flasher_url": FLASHER_URL},
            errors=errors or {},
        )

    def _pairing_progress(self, step_id: str, address: str, name: str, pin: int | None = None) -> ConfigFlowResult:
        """Start the pairing task once, show progress until it is done, then go to ``<step_id>_done``."""
        if self._pair_task is None:
            assert pin is not None
            self._pair_pin = pin
            self._pair_task = self.hass.async_create_task(self._async_pair_any(address, name, pin))
        if not self._pair_task.done():
            return self.async_show_progress(
                step_id=step_id,
                progress_action="pairing",
                progress_task=self._pair_task,
                description_placeholders={"name": name},
            )
        return self.async_show_progress_done(next_step_id=f"{step_id}_done")

    def _take_outcome(self) -> tuple[PairOutcome, int]:
        assert self._pair_task is not None
        outcome, pin = self._pair_task.result(), self._pair_pin
        self._pair_task = None
        return outcome, pin

    async def _async_pair_any(self, address: str, name: str, pin: int) -> PairOutcome:
        """Pair through the best proxy that offers pair_pvvx and verify over the encrypted link, pinned to that proxy. Only that
        proxy: a sensor that still refuses after a correct pairing has a problem of its own, and every extra proxy would add a
        bond to its small table."""
        outcome = PairOutcome()
        proxies = pairing_proxies(self.hass, address)
        if not proxies:
            outcome.error = "no_pairing_proxy"
            return outcome
        proxy = proxies[0]
        pair_result: str | None = None
        try:
            async with get_shared(self.hass).lock:
                pair_result = await async_pair(self.hass, proxy, address, pin)
                async with async_connected(
                    self.hass, address, name, encrypt=True, bonded_scanner=proxy.source
                ) as client:
                    ver, hw = await read_cfg(client)
        except PairingRequiredError:
            # the proxy paired (or tried to) but the sensor still refuses. If the sensor never asked for the PIN it paired
            # Just Works and handed out a key it then refuses; otherwise the pairing finished and the key is still refused
            outcome.error = "pair_no_passkey" if pair_result == "no passkey asked" else "pin_wrong"
            _LOGGER.info(
                "%s: pairing through %s finished but the sensor still refuses (%s)", address, proxy.name, outcome.error
            )
            return outcome
        except NotPvvxError:
            outcome.abort = "not_pvvx"
            return outcome
        except PairConnectError as err:
            _LOGGER.debug("%s", err)
            outcome.error = "pair_connect_failed"
            return outcome
        except (HomeAssistantError, OtaError) as err:
            _LOGGER.debug("Could not pair %s through %s: %s", address, proxy.name, err)
            outcome.error = "cannot_connect"
            return outcome
        if ver is None:
            outcome.abort = "not_pvvx"
            return outcome
        _LOGGER.info("%s paired through %s", address, proxy.name)
        outcome.ok = (ver, hw, proxy.source)
        return outcome

    def _pair_failed(self, address: str, entry_title: str) -> None:
        """The second failed verification after pairing leaves a repair issue behind (a failed flow creates no entry, so no entities
        and no other trace): name, address and what to try. It is removed when the sensor is added."""
        self._pair_failures += 1
        if self._pair_failures >= 2:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"pin_pairing_failed_{address}",
                is_fixable=False,
                is_persistent=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="pin_pairing_failed",
                translation_placeholders={"name": entry_title, "address": address, "flasher_url": FLASHER_URL},
            )

    def _pairing_ok(self, address: str) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, f"pin_pairing_failed_{address}")

    def _failed_form(self, step_id: str, address: str, name: str, outcome: PairOutcome) -> ConfigFlowResult:
        if outcome.abort:
            return self.async_abort(reason=outcome.abort)
        if outcome.error in ("pin_wrong", "pair_no_passkey"):
            self._pair_failed(address, name)
        return self._pin_form(step_id, name, {"base": outcome.error or "cannot_connect"})

    # new sensor
    async def async_step_pin(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The sensor has a PIN and no bond yet: pair it through a proxy that offers ``pair_pvvx``."""
        assert self._pin_target is not None
        address, name = self._pin_target
        if user_input is not None or self._pair_task is not None:
            return self._pairing_progress(
                "pin", address, name, None if user_input is None else int(user_input[CONF_PIN])
            )
        return self._pin_form("pin", name)

    async def async_step_pin_done(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._pin_target is not None
        address, name = self._pin_target
        outcome, pin = self._take_outcome()
        if outcome.ok is None:
            return self._failed_form("pin", address, name, outcome)
        ver, hw, source = outcome.ok
        self._pairing_ok(address)
        return self.async_create_entry(
            title=name,
            data={
                CONF_ADDRESS: address,
                CONF_VERSION: ver,
                CONF_HW_ID: hw,
                CONF_PIN_BONDED: True,
                CONF_BONDED_SCANNER: source,
                CONF_PIN: pin,
            },
        )

    # reauthentication / reconfiguration: (re)enter the PIN of a sensor that already has an entry
    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The bond of a PIN sensor is dead and no PIN is stored: ask for it."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_pin_for_entry("reauth_confirm", self._get_reauth_entry(), user_input)

    async def async_step_reauth_confirm_done(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self._entry_done("reauth_confirm", self._get_reauth_entry())

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Store (or change) the PIN of a sensor and pair it through a proxy. A sensor that is not bonded yet (a PIN was set
        after it was added) is paired here for the first time; that needs a proxy that offers the pairing action."""
        entry = self._get_reconfigure_entry()
        if (
            self._pair_task is None
            and not entry.data.get(CONF_PIN_BONDED)
            and not pairing_proxies(self.hass, str(entry.data[CONF_ADDRESS]))
        ):
            return self.async_abort(reason="no_pairing_proxy")
        return await self._async_pin_for_entry("reconfigure", entry, user_input)

    async def async_step_reconfigure_done(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self._entry_done("reconfigure", self._get_reconfigure_entry())

    async def _async_pin_for_entry(
        self, step_id: str, entry: ConfigEntry, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        if user_input is not None or self._pair_task is not None:
            return self._pairing_progress(
                step_id,
                str(entry.data[CONF_ADDRESS]),
                entry.title,
                None if user_input is None else int(user_input[CONF_PIN]),
            )
        return self._pin_form(step_id, entry.title)

    def _entry_done(self, step_id: str, entry: ConfigEntry) -> ConfigFlowResult:
        address = str(entry.data[CONF_ADDRESS])
        outcome, pin = self._take_outcome()
        if outcome.ok is None:
            return self._failed_form(step_id, address, entry.title, outcome)
        ver, hw, source = outcome.ok
        self._pairing_ok(address)
        for key in ("pin_set", "pin_bond_lost"):
            ir.async_delete_issue(self.hass, DOMAIN, f"{key}_{entry.entry_id}")
        return self.async_update_reload_and_abort(
            entry,
            data_updates={
                CONF_VERSION: ver,
                CONF_HW_ID: hw,
                CONF_PIN_BONDED: True,
                CONF_BONDED_SCANNER: source,
                CONF_PIN: pin,
            },
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick one of the candidate sensors currently in range (verified by connecting)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            result = await self._async_verify_and_create(self._found[address], errors)
            if result is not None:
                return result
        else:
            current = self._async_current_ids()
            self._found = {
                i.address.upper(): i
                for i in async_discovered_service_info(self.hass, connectable=True)
                if candidate(i) and i.address.upper() not in current
            }
        if not self._found:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(
                {vol.Required(CONF_ADDRESS): vol.In({a: display_name(self.hass, i) for a, i in self._found.items()})}
            ),
            errors=errors,
            description_placeholders={"error": self._error_detail},
        )


class PvvxOptionsFlow(OptionsFlow):
    """Per sensor options. The Beta firmware switch also lives in the entry options and is kept untouched."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={**self.config_entry.options, **user_input})
        opts = self.config_entry.options
        schema = _schema(
            {
                vol.Required(
                    CONF_MIN_BATTERY_V, default=opts.get(CONF_MIN_BATTERY_V, MIN_BATTERY_V)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1.8, max=3.2, step=0.05, unit_of_measurement="V", mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_AUTO_CLOCK_SYNC, default=opts.get(CONF_AUTO_CLOCK_SYNC, True)
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_AUTO_RESTORE_ENCRYPTION, default=opts.get(CONF_AUTO_RESTORE_ENCRYPTION, True)
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_POLL_INTERVAL, default=opts.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(POLL_CHOICES),
                        translation_key="poll_interval",
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
