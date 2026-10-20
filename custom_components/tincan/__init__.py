"""Tin Can kids-phone integration (cloud polling, read-only)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TinCanClient
from .const import CONF_REFRESH_TOKEN
from .coordinator import TinCanCallCoordinator, TinCanCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


@dataclass
class TinCanRuntimeData:
    """Objects shared by the platforms of one config entry."""

    client: TinCanClient
    account: TinCanCoordinator
    calls: TinCanCallCoordinator


TinCanConfigEntry = ConfigEntry[TinCanRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: TinCanConfigEntry) -> bool:
    """Set up Tin Can from a config entry."""

    @callback
    def _persist_refresh_token(token: str | None) -> None:
        if token and token != entry.data.get(CONF_REFRESH_TOKEN):
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_REFRESH_TOKEN: token}
            )

    client = TinCanClient(
        async_get_clientsession(hass),
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        on_token_update=_persist_refresh_token,
    )

    account = TinCanCoordinator(hass, entry, client)
    await account.async_config_entry_first_refresh()

    calls = TinCanCallCoordinator(hass, entry, client, account)
    await calls.async_config_entry_first_refresh()

    entry.runtime_data = TinCanRuntimeData(client=client, account=account, calls=calls)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TinCanConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
