"""Config flow for Tin Can: email + password, reauth, and polling options."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import TinCanAuthError, TinCanClient, TinCanConnectionError, TinCanError
from .const import (
    CONF_CALL_POLL_INTERVAL,
    CONF_POLL_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_RING_WINDOW,
    DEFAULT_CALL_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_RING_WINDOW,
    DOMAIN,
    MAX_CALL_POLL_INTERVAL,
    MAX_POLL_INTERVAL,
    MAX_RING_WINDOW,
    MIN_CALL_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    MIN_RING_WINDOW,
)

_LOGGER = logging.getLogger(__name__)

EMAIL_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
)
PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): EMAIL_SELECTOR,
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
    }
)
STEP_REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR})


def _seconds(minimum: int, maximum: int) -> Any:
    return vol.All(
        NumberSelector(
            NumberSelectorConfig(
                min=minimum, max=maximum, step=1, mode=NumberSelectorMode.BOX,
                unit_of_measurement="s",
            )
        ),
        vol.Coerce(int),
    )


OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): _seconds(
            MIN_POLL_INTERVAL, MAX_POLL_INTERVAL
        ),
        vol.Required(
            CONF_CALL_POLL_INTERVAL, default=DEFAULT_CALL_POLL_INTERVAL
        ): _seconds(MIN_CALL_POLL_INTERVAL, MAX_CALL_POLL_INTERVAL),
        vol.Required(CONF_RING_WINDOW, default=DEFAULT_RING_WINDOW): _seconds(
            MIN_RING_WINDOW, MAX_RING_WINDOW
        ),
    }
)


async def _async_validate(hass: HomeAssistant, email: str, password: str) -> str | None:
    """Log in with the given credentials; return the refresh token."""
    client = TinCanClient(async_get_clientsession(hass), email, password)
    await client.async_login()
    return client.refresh_token


class TinCanConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Tin Can config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._reauth_email: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> TinCanOptionsFlow:
        return TinCanOptionsFlow()

    async def _try_login(self, email: str, password: str) -> tuple[str | None, str | None]:
        """Return (refresh_token, error_key)."""
        try:
            return await _async_validate(self.hass, email, password), None
        except TinCanAuthError:
            return None, "invalid_auth"
        except TinCanConnectionError:
            return None, "cannot_connect"
        except TinCanError:
            _LOGGER.exception("Unexpected Tin Can error during login")
            return None, "unknown"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip().lower()
            await self.async_set_unique_id(email)
            self._abort_if_unique_id_configured()
            refresh_token, error = await self._try_login(email, user_input[CONF_PASSWORD])
            if error is None:
                return self.async_create_entry(
                    title=email,
                    data={
                        CONF_EMAIL: email,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_REFRESH_TOKEN: refresh_token,
                    },
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._reauth_email = entry_data[CONF_EMAIL]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        email = self._reauth_email or ""
        if user_input is not None:
            refresh_token, error = await self._try_login(email, user_input[CONF_PASSWORD])
            if error is None:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_REFRESH_TOKEN: refresh_token,
                    },
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={CONF_EMAIL: email},
            errors=errors,
        )


class TinCanOptionsFlow(OptionsFlowWithReload):
    """Polling intervals and the Recent Call window."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, self.config_entry.options
            ),
        )
