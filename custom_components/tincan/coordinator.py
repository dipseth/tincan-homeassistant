"""Data coordinators for Tin Can.

Two coordinators per account:
  * TinCanCoordinator      slow poll: subscriptions, devices, contacts, presence
  * TinCanCallCoordinator  fast poll: latest call per line (near-live ring)

Call lifecycle (observed with controlled test calls, 2026-10-20). The cloud
writes the call record the moment the phone starts ringing and rewrites it as
the call progresses, so no single snapshot says whether a call was missed:

    answered:    missed/0s (ringing) -> completed/0s (talking) -> completed/Ns
    unanswered:  missed/0s (ringing) -> completed/0s (ringing out, ~48 s)
                 -> missed/0s (final, ~64 s)

CallTracker follows each call across polls to tell these apart.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import TinCanAuthError, TinCanClient, TinCanError
from .const import (
    CONF_CALL_POLL_INTERVAL,
    CONF_POLL_INTERVAL,
    CONF_RING_WINDOW,
    DEFAULT_CALL_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_RING_WINDOW,
    CDR_FETCH_LIMIT,
    DOMAIN,
    EVENT_CALL,
    FINAL_PHASES,
    MAX_CALL_SECONDS,
    PHASE_CONNECTED,
    PHASE_ENDED,
    PHASE_MISSED,
    PHASE_RINGING,
    RING_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------- helpers
def digits(value: Any) -> str:
    """Last 10 digits of a phone number (+1XXXXXXXXXX == XXXXXXXXXX)."""
    return "".join(ch for ch in str(value or "") if ch.isdigit())[-10:]


def build_contact_index(contacts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index the approved contact book by routing id (dial_plan) and phone number."""
    index: dict[str, dict[str, Any]] = {}
    for contact in contacts or []:
        base = {
            "contact_name": contact.get("name"),
            "contact_avatar": contact.get("avatar"),
            "contact_id": contact.get("id"),
            "contact_status": contact.get("status"),
        }
        for number in contact.get("contact_numbers") or []:
            summary = dict(
                base,
                speeddial=number.get("favorite"),
                dial_plan=number.get("dial_plan"),
                phone_number=number.get("phone_number"),
                number_label=number.get("label"),
                group_id=number.get("group_id"),
            )
            if number.get("dial_plan"):
                index[str(number["dial_plan"])] = summary
            if number.get("phone_number"):
                index[digits(number["phone_number"])] = summary
    return index


def parse_time(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp from the API into an aware datetime."""
    if not value:
        return None
    parsed = dt_util.parse_datetime(str(value))
    if parsed is None:
        return None
    return dt_util.as_utc(parsed) if parsed.tzinfo else parsed.replace(tzinfo=dt_util.UTC)


@dataclass
class CallInfo:
    """One call-log record, enriched by matching the other party to the contact book."""

    record: dict[str, Any]
    other_party_name: str
    other_party_number: str | None
    routing_id: str | None
    avatar: str | None
    contact: dict[str, Any]
    start: datetime | None
    end: datetime | None
    phase: str = PHASE_ENDED
    key: str | None = None

    @property
    def final(self) -> bool:
        """True once the call has ended or been missed (it will not change again)."""
        return self.phase in FINAL_PHASES

    @property
    def call_id(self) -> str | None:
        return self.record.get("call_id")

    @property
    def api_call_type(self) -> str:
        """The label exactly as the API reports it."""
        return str(self.record.get("call_type") or "")

    @property
    def is_tincan(self) -> bool:
        return bool(self.record.get("other_party_is_tincan"))

    @property
    def via_carrier(self) -> bool:
        """True when the record came in through the SIP carrier (call_id like "...@host").

        Only inbound calls from ordinary phones take that path; Tin Can-to-Tin Can
        calls and anything the kid dials out get an opaque token / UUID call_id.
        """
        return "@" in str(self.record.get("call_id") or "")

    @property
    def direction(self) -> str:
        """Direction from the kid's phone's point of view: incoming | outgoing | unknown.

        Verified with controlled test calls (2026-10-20): a regular phone ringing
        the Tin Can is logged as "outgoing" with the caller under to_*, but its
        call_id is SIP-style ("...@host"), so that format wins over the label.
        Every other record is labelled correctly: "incoming"/"missed" carry the
        caller under from_*, "outgoing" carries the callee under to_*.
        """
        if self.via_carrier:
            return "incoming"
        label = self.api_call_type
        if label in ("incoming", "missed"):
            return "incoming"
        if label == "outgoing":
            return "outgoing"
        return "unknown"

    @property
    def missed(self) -> bool:
        """Only true once the call is over: a ringing call is also logged "missed"."""
        return self.phase == PHASE_MISSED

    @property
    def status(self) -> str:
        return str(self.record.get("status") or "")

    @property
    def summary(self) -> str:
        if not self.final:
            label = "ringing" if self.phase == PHASE_RINGING else "in call"
            return f"{self.other_party_name} ({self.direction}, {label})"
        return f"{self.other_party_name} ({self.direction}, {self.status})"

    def attributes(self) -> dict[str, Any]:
        rec = self.record
        return {
            "call_id": rec.get("call_id"),
            "direction": self.direction,
            "api_call_type": self.api_call_type,
            "direction_source": "call_id" if self.via_carrier else "call_type",
            "phase": self.phase,
            "missed": self.missed,
            "status": self.status,
            "duration": rec.get("duration"),
            "start_time": rec.get("start_time"),
            "end_time": self.end.isoformat() if self.end else None,
            "other_party_name": self.other_party_name,
            "other_party_number": self.other_party_number,
            "routing_id": self.routing_id,
            "other_party_is_tincan": rec.get("other_party_is_tincan"),
            "contact_name": self.contact.get("contact_name"),
            "contact_id": self.contact.get("contact_id"),
            "contact_status": self.contact.get("contact_status"),
            "speeddial": self.contact.get("speeddial"),
            "group_id": self.contact.get("group_id"),
            "number_label": self.contact.get("number_label"),
            "avatar": self.avatar,
        }


def call_phase(
    record: dict[str, Any],
    start: datetime | None,
    now: datetime,
    saw_connected: bool = False,
) -> str:
    """Where a call is in its lifecycle, given the record and what we saw before.

    `saw_connected` means an earlier poll saw this call at "completed": after
    that, "missed" is the final verdict rather than the initial ringing state.
    Without that history, a "missed" record only counts as final once it is
    older than RING_TIMEOUT.
    """
    status = str(record.get("status") or "")
    try:
        duration = int(record.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    age = (now - start).total_seconds() if start is not None else None

    if status == "completed":
        if duration > 0 or (age is not None and age > MAX_CALL_SECONDS):
            return PHASE_ENDED
        return PHASE_CONNECTED
    if status in ("missed", "voicemail") or record.get("call_type") == "missed":
        if saw_connected or age is None or age >= RING_TIMEOUT:
            return PHASE_MISSED
        return PHASE_RINGING
    if duration > 0 or age is None or age >= RING_TIMEOUT:
        return PHASE_ENDED
    return PHASE_RINGING


def merge_legs(records: list[dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    """Collapse the newest call's records (one per call leg) into one record.

    A single call can be logged several times with different call_ids but the
    same start second. Keyed on that start time, the merged record takes the
    most-progressed status and longest duration, and a stable call_id (the
    carrier/SIP one if present, since direction detection relies on it).
    """
    if not records:
        return None
    key = str(records[0].get("start_time") or "")[:19]
    legs = [r for r in records if str(r.get("start_time") or "")[:19] == key]

    def _duration(rec: dict[str, Any]) -> int:
        try:
            return int(rec.get("duration") or 0)
        except (TypeError, ValueError):
            return 0

    def _progress(rec: dict[str, Any]) -> tuple:
        cid = str(rec.get("call_id") or "")
        return (rec.get("status") == "completed", _duration(rec), "@" in cid, cid)

    merged = dict(max(legs, key=_progress))
    ids = sorted(str(r.get("call_id")) for r in legs if r.get("call_id"))
    carrier = [cid for cid in ids if "@" in cid]
    if ids:
        merged["call_id"] = carrier[0] if carrier else ids[0]
    merged["duration"] = max(_duration(r) for r in legs)
    if any(r.get("status") == "completed" for r in legs):
        merged["status"] = "completed"
    return key, merged


def enrich_call(
    record: dict[str, Any],
    contact_index: dict[str, dict[str, Any]],
    now: datetime | None = None,
    saw_connected: bool = False,
) -> CallInfo:
    """Resolve the other party of a call record against the contact book.

    The other party is whichever side the record fills in (the kid's own line is
    never listed): from_* for API "incoming"/"missed" records, to_* for "outgoing".
    Match by routing id (== a contact number's dial_plan) first, then by phone
    number (last 10 digits).
    """
    side = "to" if record.get("call_type") == "outgoing" else "from"
    if not any(record.get(f"{side}_{k}") for k in ("routing_id", "number", "name")):
        side = "from" if side == "to" else "to"  # fall back to whichever side is populated
    routing_id = record.get(f"{side}_routing_id")
    number = record.get(f"{side}_number")
    name = record.get(f"{side}_name")
    avatar = record.get(f"{side}_avatar")

    match: dict[str, Any] = {}
    if routing_id is not None:
        match = contact_index.get(str(routing_id), {})
    if not match and number:
        match = contact_index.get(digits(number), {})

    start = parse_time(record.get("start_time"))
    phase = call_phase(record, start, now or dt_util.utcnow(), saw_connected)
    end = None
    if start is not None and phase in FINAL_PHASES:
        try:
            end = start + timedelta(seconds=int(record.get("duration") or 0))
        except (TypeError, ValueError):
            end = start

    return CallInfo(
        record=record,
        other_party_name=match.get("contact_name") or name or number or "Unknown",
        other_party_number=number,
        routing_id=str(routing_id) if routing_id is not None else None,
        avatar=match.get("contact_avatar") or avatar,
        contact=match,
        start=start,
        end=end,
        phase=phase,
    )


class CallTracker:
    """Follows one line's newest call across polls.

    Remembers whether the call has been seen at "completed" (so a later
    "missed" is final, not ringing), records when it was first seen final
    (the real end time: the API's duration only counts talk time), and says
    when to fire the tincan_call event: once per call, when it turns final,
    and never for a call that was already over when tracking started.
    """

    def __init__(self) -> None:
        self._primed = False
        self._connected: set[str] = set()
        self._ended_at: dict[str, datetime] = {}
        self._fired_key: str | None = None

    def update(
        self,
        records: list[dict[str, Any]],
        contact_index: dict[str, dict[str, Any]],
        now: datetime,
    ) -> tuple[CallInfo | None, bool]:
        """Return (newest call, whether to fire the event for it now)."""
        merged = merge_legs(records)
        if merged is None:
            self._primed = True
            return None, False
        key, record = merged
        if record.get("status") == "completed":
            self._connected.add(key)
        call = enrich_call(record, contact_index, now, key in self._connected)
        call.key = key

        fire = False
        if call.final:
            if key not in self._ended_at:
                # watched it end live; a call already over at startup keeps
                # the API-derived end (start + duration) instead
                self._ended_at[key] = now if self._primed else call.end
            call.end = self._ended_at[key]
            fire = self._primed and key != self._fired_key
            self._fired_key = key
        self._primed = True

        # only the newest call matters; forget older ones
        self._connected &= {key}
        self._ended_at = {k: v for k, v in self._ended_at.items() if k == key}
        return call, fire


# --------------------------------------------------------------------- data
@dataclass
class TinCanLine:
    """Everything the slow poll knows about one Tin Can line (subscription)."""

    subscription_id: str
    subscription: dict[str, Any]
    device: dict[str, Any] | None
    contacts: list[dict[str, Any]]
    status: dict[str, Any] | None
    contact_index: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def name(self) -> str:
        if self.device and self.device.get("name"):
            return str(self.device["name"])
        caller_id = self.subscription.get("caller_id")
        return f"{caller_id}'s Tin Can" if caller_id else "Tin Can"

    @property
    def mac_address(self) -> str | None:
        return (self.device or {}).get("mac_address")

    @property
    def online(self) -> bool | None:
        if not self.status or not self.status.get("device_status"):
            return None
        return self.status["device_status"] == "online"

    @property
    def dnd(self) -> bool | None:
        value = self.subscription.get("dnd_enabled")
        return None if value is None else bool(value)


# --------------------------------------------------------------------- slow
class TinCanCoordinator(DataUpdateCoordinator[dict[str, TinCanLine]]):
    """Polls subscriptions, devices, contacts and presence."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: TinCanClient) -> None:
        interval = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} account",
            config_entry=entry,
            update_interval=timedelta(seconds=interval),
        )
        self.client = client

    async def _async_update_data(self) -> dict[str, TinCanLine]:
        previous = self.data or {}
        try:
            subscriptions, devices = await asyncio.gather(
                self.client.async_subscriptions(), self.client.async_devices()
            )
        except TinCanAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except TinCanError as err:
            raise UpdateFailed(str(err)) from err

        device_by_sub = {d.get("subscription_id"): d for d in devices if d.get("subscription_id")}

        lines: dict[str, TinCanLine] = {}
        for sub in subscriptions:
            sid = sub.get("subscription_id")
            if not sid or sub.get("archived_at"):
                continue
            device = device_by_sub.get(sid)
            if device is None and len(devices) == 1 and len(subscriptions) == 1:
                device = devices[0]  # single phone, single line: pair them
            old = previous.get(sid)

            contacts_task = self.client.async_contacts(sid)
            status_task = (
                self.client.async_device_status(
                    device["mac_address"], device.get("device_type") or "TINCAN"
                )
                if device and device.get("mac_address")
                else None
            )
            results = await asyncio.gather(
                contacts_task, *([status_task] if status_task else []), return_exceptions=True
            )

            contacts_result = results[0]
            if isinstance(contacts_result, TinCanAuthError):
                raise ConfigEntryAuthFailed(str(contacts_result)) from contacts_result
            if isinstance(contacts_result, BaseException):
                _LOGGER.warning("Contacts poll failed for %s: %s", sid, contacts_result)
                contacts = old.contacts if old else []
            else:
                contacts = contacts_result

            status: dict[str, Any] | None = old.status if old else None
            if status_task:
                status_result = results[1]
                if isinstance(status_result, TinCanAuthError):
                    raise ConfigEntryAuthFailed(str(status_result)) from status_result
                if isinstance(status_result, BaseException):
                    _LOGGER.warning("Presence poll failed for %s: %s", sid, status_result)
                else:
                    status = status_result

            lines[sid] = TinCanLine(
                subscription_id=sid,
                subscription=sub,
                device=device,
                contacts=contacts,
                status=status,
                contact_index=build_contact_index(contacts),
            )

        if not lines:
            raise UpdateFailed("No active Tin Can subscriptions on this account")
        return lines


# --------------------------------------------------------------------- fast
class TinCanCallCoordinator(DataUpdateCoordinator[dict[str, CallInfo | None]]):
    """Polls only the newest call record per line, so ring detection is quick."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: TinCanClient,
        account: TinCanCoordinator,
    ) -> None:
        interval = entry.options.get(CONF_CALL_POLL_INTERVAL, DEFAULT_CALL_POLL_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} calls",
            config_entry=entry,
            update_interval=timedelta(seconds=interval),
        )
        self.client = client
        self.account = account
        self.ring_window = timedelta(
            seconds=entry.options.get(CONF_RING_WINDOW, DEFAULT_RING_WINDOW)
        )
        self._trackers: dict[str, CallTracker] = {}

    def _line(self, sid: str) -> TinCanLine | None:
        return (self.account.data or {}).get(sid)

    async def _async_update_data(self) -> dict[str, CallInfo | None]:
        lines = self.account.data or {}
        previous = self.data or {}
        results = await asyncio.gather(
            *(self.client.async_cdrs(sid, limit=CDR_FETCH_LIMIT) for sid in lines),
            return_exceptions=True,
        )
        now = dt_util.utcnow()

        calls: dict[str, CallInfo | None] = {}
        failures = 0
        for sid, result in zip(lines, results, strict=True):
            if isinstance(result, TinCanAuthError):
                raise ConfigEntryAuthFailed(str(result)) from result
            if isinstance(result, BaseException):
                failures += 1
                _LOGGER.debug("Call-log poll failed for %s: %s", sid, result)
                calls[sid] = previous.get(sid)
                continue
            tracker = self._trackers.setdefault(sid, CallTracker())
            calls[sid], fire = tracker.update(result, lines[sid].contact_index, now)
            if fire and calls[sid] is not None:
                self._fire_event(sid, calls[sid])

        if lines and failures == len(lines):
            raise UpdateFailed("Call-log poll failed for every line")
        return calls

    def _fire_event(self, sid: str, call: CallInfo) -> None:
        """Fire tincan_call for a call that has just ended or been missed."""
        line = self._line(sid)
        self.hass.bus.async_fire(
            EVENT_CALL,
            {
                "subscription_id": sid,
                "device_name": line.name if line else None,
                **call.attributes(),
            },
        )

    def recent_call_active(self, call: CallInfo | None) -> tuple[bool, int | None]:
        """Return (is_recent, seconds_since_end) for the Recent Call sensor."""
        if call is None or call.end is None:
            return False, None
        since = int((dt_util.utcnow() - call.end).total_seconds())
        return 0 <= since <= self.ring_window.total_seconds(), since

    def recent_call_expiry(self, call: CallInfo | None) -> datetime | None:
        """When the Recent Call flag should drop back to off."""
        if call is None or call.end is None:
            return None
        return call.end + self.ring_window
