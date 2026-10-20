"""Data coordinators for Tin Can.

Two coordinators per account:
  * TinCanCoordinator      slow poll: subscriptions, devices, contacts, presence
  * TinCanCallCoordinator  fast poll: latest call record per line (near-live ring)
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
    DOMAIN,
    EVENT_CALL,
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
        return self.status in ("missed", "voicemail") or self.api_call_type == "missed"

    @property
    def status(self) -> str:
        return str(self.record.get("status") or "")

    @property
    def summary(self) -> str:
        return f"{self.other_party_name} ({self.direction}, {self.status})"

    def attributes(self) -> dict[str, Any]:
        rec = self.record
        return {
            "call_id": rec.get("call_id"),
            "direction": self.direction,
            "api_call_type": self.api_call_type,
            "direction_source": "call_id" if self.via_carrier else "call_type",
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


def enrich_call(record: dict[str, Any], contact_index: dict[str, dict[str, Any]]) -> CallInfo:
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
    end = None
    if start is not None:
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
    )


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
        self._seen: dict[str, str | None] = {}

    def _line(self, sid: str) -> TinCanLine | None:
        return (self.account.data or {}).get(sid)

    async def _async_update_data(self) -> dict[str, CallInfo | None]:
        lines = self.account.data or {}
        previous = self.data or {}
        results = await asyncio.gather(
            *(self.client.async_cdrs(sid, limit=1) for sid in lines), return_exceptions=True
        )

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
            record = result[0] if result else None
            calls[sid] = (
                enrich_call(record, lines[sid].contact_index) if record else None
            )
            self._maybe_fire_event(sid, calls[sid])

        if lines and failures == len(lines):
            raise UpdateFailed("Call-log poll failed for every line")
        return calls

    def _maybe_fire_event(self, sid: str, call: CallInfo | None) -> None:
        """Fire tincan_call once per new call record (not on the first poll)."""
        call_id = call.call_id if call else None
        first_poll = sid not in self._seen
        changed = self._seen.get(sid) != call_id
        self._seen[sid] = call_id
        if first_poll or not changed or call is None:
            return
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
