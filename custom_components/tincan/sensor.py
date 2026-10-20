"""Sensors: Call State, Last Call, Last Call Time, Phone Number, Approved Contacts, Subscription, Provisioning."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TinCanConfigEntry
from .const import PHASE_CONNECTED, PHASE_RINGING
from .entity import TinCanCallEntity, TinCanEntity

CALL_STATE_IDLE = "idle"

SUBSCRIPTION_ATTRS = (
    "subscription_id", "type", "provider", "stripe_subscription_id", "phone_number",
    "internal_id", "created_at", "caller_id", "avatar", "dnd_enabled",
    "emergency_enabled", "emergency_status", "onboarding_complete",
    "primary_device_type", "is_v2",
)
PHONE_ATTRS = (
    "phone_number", "provider", "type", "phone_number_id", "emergency_address",
    "internal_id", "subscription_id", "created_at",
)
DEVICE_ATTRS = (
    "device_id", "name", "mac_address", "device_type", "avatar", "created_at", "updated_at",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TinCanConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create sensors for every Tin Can line."""
    data = entry.runtime_data
    entities: list[SensorEntity] = []
    for sid in data.account.data:
        entities.extend(
            [
                TinCanPhoneNumberSensor(data.account, sid),
                TinCanContactsSensor(data.account, sid),
                TinCanSubscriptionSensor(data.account, sid),
                TinCanProvisioningSensor(data.account, sid),
                TinCanCallStateSensor(data.calls, sid),
                TinCanLastCallSensor(data.calls, sid),
                TinCanLastCallTimeSensor(data.calls, sid),
            ]
        )
    async_add_entities(entities)


class TinCanPhoneNumberSensor(TinCanEntity, SensorEntity):
    """The line's phone number (DID)."""

    _attr_icon = "mdi:phone"

    def __init__(self, coordinator, subscription_id: str) -> None:
        super().__init__(coordinator, subscription_id, "phone_number")

    @property
    def native_value(self) -> str | None:
        return self.line.subscription.get("phone_number") if self.line else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        sub = self.line.subscription if self.line else {}
        return {k: sub.get(k) for k in PHONE_ATTRS}


class TinCanContactsSensor(TinCanEntity, SensorEntity):
    """Number of approved contacts, with the names as an attribute."""

    _attr_icon = "mdi:contacts"
    _attr_native_unit_of_measurement = "contacts"

    def __init__(self, coordinator, subscription_id: str) -> None:
        super().__init__(coordinator, subscription_id, "approved_contacts")

    def _names(self) -> list[str]:
        contacts = self.line.contacts if self.line else []
        return sorted({str(c.get("name", "")).strip() for c in contacts if c.get("name")})

    @property
    def native_value(self) -> int:
        return len(self._names())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        contacts = self.line.contacts if self.line else []
        speeddials = {}
        for contact in contacts:
            for number in contact.get("contact_numbers") or []:
                if number.get("favorite"):
                    speeddials[number["favorite"]] = contact.get("name")
        return {"names": self._names(), "speeddials": dict(sorted(speeddials.items()))}


class TinCanSubscriptionSensor(TinCanEntity, SensorEntity):
    """Subscription state plus plan / emergency details."""

    _attr_icon = "mdi:card-account-details"

    def __init__(self, coordinator, subscription_id: str) -> None:
        super().__init__(coordinator, subscription_id, "subscription")

    @property
    def native_value(self) -> str:
        sub = self.line.subscription if self.line else {}
        if not sub.get("subscription_id"):
            return "unknown"
        return "archived" if sub.get("archived_at") else "active"

    @property
    def entity_picture(self) -> str | None:
        """The kid's own avatar, as chosen in the Tin Can app."""
        avatar = (self.line.subscription if self.line else {}).get("avatar")
        return avatar if isinstance(avatar, str) and avatar.startswith("http") else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        sub = self.line.subscription if self.line else {}
        return {k: sub.get(k) for k in SUBSCRIPTION_ATTRS}


class TinCanProvisioningSensor(TinCanEntity, SensorEntity):
    """Device provisioning status from /account/devices."""

    _attr_icon = "mdi:cellphone-check"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, subscription_id: str) -> None:
        super().__init__(coordinator, subscription_id, "provisioning")

    @property
    def available(self) -> bool:
        return super().available and self.line.device is not None

    @property
    def native_value(self) -> str | None:
        device = self.line.device if self.line else None
        return device.get("provisioning_status") if device else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        device = (self.line.device if self.line else None) or {}
        return {k: device.get(k) for k in DEVICE_ATTRS}


class TinCanCallStateSensor(TinCanCallEntity, SensorEntity):
    """idle / ringing / in_call, from the newest call's lifecycle phase.

    Lags the phone by up to one call poll. An unanswered call also reads
    in_call for ~15 s while it rings out, before it settles as missed.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [CALL_STATE_IDLE, PHASE_RINGING, PHASE_CONNECTED]

    def __init__(self, coordinator, subscription_id: str) -> None:
        super().__init__(coordinator, subscription_id, "call_state")

    @property
    def native_value(self) -> str:
        if self.call is None or self.call.final:
            return CALL_STATE_IDLE
        return self.call.phase

    @property
    def icon(self) -> str:
        return {
            PHASE_RINGING: "mdi:phone-ring",
            PHASE_CONNECTED: "mdi:phone-in-talk",
        }.get(self.native_value, "mdi:phone-hangup")

    @property
    def entity_picture(self) -> str | None:
        return self.call.avatar if self.call and not self.call.final else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.call is None or self.call.final:
            return {}
        return self.call.attributes()


class TinCanLastCallSensor(TinCanCallEntity, SensorEntity):
    """Caller-ID text of the newest call, with the caller's avatar as entity picture."""

    _attr_icon = "mdi:phone-log"

    def __init__(self, coordinator, subscription_id: str) -> None:
        super().__init__(coordinator, subscription_id, "last_call")

    @property
    def native_value(self) -> str | None:
        return self.call.summary if self.call else None

    @property
    def entity_picture(self) -> str | None:
        return self.call.avatar if self.call else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.call.attributes() if self.call else {}


class TinCanLastCallTimeSensor(TinCanCallEntity, SensorEntity):
    """Start time of the newest call."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, subscription_id: str) -> None:
        super().__init__(coordinator, subscription_id, "last_call_time")

    @property
    def native_value(self) -> datetime | None:
        return self.call.start if self.call else None
