"""Constants for the Tin Can integration."""

from __future__ import annotations

DOMAIN = "tincan"
MANUFACTURER = "Tin Can"

CONF_REFRESH_TOKEN = "refresh_token"
CONF_POLL_INTERVAL = "poll_interval"
CONF_CALL_POLL_INTERVAL = "call_poll_interval"
CONF_RING_WINDOW = "ring_window"

DEFAULT_POLL_INTERVAL = 30  # devices / subscriptions / contacts / presence
DEFAULT_CALL_POLL_INTERVAL = 8  # call log only (near-live "Recent Call")
DEFAULT_RING_WINDOW = 30  # seconds Recent Call stays on after a call ends

MIN_POLL_INTERVAL = 10
MAX_POLL_INTERVAL = 600
MIN_CALL_POLL_INTERVAL = 3
MAX_CALL_POLL_INTERVAL = 120
MIN_RING_WINDOW = 5
MAX_RING_WINDOW = 600

# Fired on the HA event bus whenever a new call-log record appears.
EVENT_CALL = f"{DOMAIN}_call"
