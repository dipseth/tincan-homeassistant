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

# How long a record may sit at "missed" (ringing) before it is trusted as a
# final missed call without having seen the ring-out. Observed 2026-10-20: an
# unanswered call is "missed" while ringing, flips to "completed" at ~48 s when
# it rings out, then settles back to "missed" at ~64 s. If the caller hangs up
# first the record never changes at all, so a call still "missed" well past the
# ~48 s ring-out was abandoned. Keep this above ring-out + LIVE_CALL_POLL_INTERVAL.
RING_TIMEOUT = 55
# Call-log poll interval while a call is ringing or in progress, so hang-up,
# pickup and ring-out are noticed quickly; the configured interval otherwise.
LIVE_CALL_POLL_INTERVAL = 3
# A record still at "completed" with duration 0 after this long is treated as
# ended (guards against a call that never got its final duration written).
MAX_CALL_SECONDS = 4 * 3600
# Call-log records fetched per poll; one call can produce several records
# (one per call leg) that share a start time and are merged into one call.
CDR_FETCH_LIMIT = 5

# Call phases (from the kid's phone's point of view)
PHASE_RINGING = "ringing"  # record exists, nobody has picked up yet
PHASE_CONNECTED = "in_call"  # answered and in progress (or briefly: ringing out)
PHASE_ENDED = "ended"  # answered call that has finished
PHASE_MISSED = "missed"  # rang out / caller gave up / voicemail
FINAL_PHASES = (PHASE_ENDED, PHASE_MISSED)

# Fired on the HA event bus once per call, when it reaches a final phase
# (ended or missed) -- never while it is still ringing or in progress.
EVENT_CALL = f"{DOMAIN}_call"
