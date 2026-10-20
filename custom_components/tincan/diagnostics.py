"""Diagnostics for Tin Can (credentials and phone numbers redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import TinCanConfigEntry
from .const import CONF_REFRESH_TOKEN

TO_REDACT = {
    "email", "password", CONF_REFRESH_TOKEN, "phone_number", "from_number", "to_number",
    "mac_address", "emergency_address", "stripe_subscription_id", "unique_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TinCanConfigEntry
) -> dict[str, Any]:
    data = entry.runtime_data
    lines = {
        sid: {
            "subscription": line.subscription,
            "device": line.device,
            "status": line.status,
            "contact_count": len(line.contacts),
        }
        for sid, line in (data.account.data or {}).items()
    }
    calls = {
        sid: (call.attributes() if call else None)
        for sid, call in (data.calls.data or {}).items()
    }
    return async_redact_data(
        {
            "entry": {"data": dict(entry.data), "options": dict(entry.options)},
            "lines": lines,
            "latest_calls": calls,
        },
        TO_REDACT,
    )
