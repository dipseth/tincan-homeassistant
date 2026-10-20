"""Binary sensors: Online, Do Not Disturb, Recent Call."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time

from . import TinCanConfigEntry
from .entity import TinCanCallEntity, TinCanEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TinCanConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create binary sensors for every Tin Can line."""
    data = entry.runtime_data
    entities: list[BinarySensorEntity] = []
    for sid in data.account.data:
        entities.append(TinCanOnlineSensor(data.account, sid))
        entities.append(TinCanDndSensor(data.account, sid))
        entities.append(TinCanRecentCallSensor(data.calls, sid))
    async_add_entities(entities)


class TinCanOnlineSensor(TinCanEntity, BinarySensorEntity):
    """Connectivity from /device/status (does not change during a call)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, subscription_id: str) -> None:
        super().__init__(coordinator, subscription_id, "online")

    @property
    def available(self) -> bool:
        return super().available and self.line.online is not None

    @property
    def is_on(self) -> bool | None:
        return self.line.online if self.line else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = (self.line.status if self.line else None) or {}
        return {
            "device_status": status.get("device_status"),
            "is_claimed": status.get("is_claimed"),
            "mac_address": status.get("mac_address"),
            "device_type": status.get("device_type"),
        }


class TinCanDndSensor(TinCanEntity, BinarySensorEntity):
    """Do Not Disturb flag from /subscriptions."""

    _attr_icon = "mdi:minus-circle"

    def __init__(self, coordinator, subscription_id: str) -> None:
        super().__init__(coordinator, subscription_id, "do_not_disturb")

    @property
    def available(self) -> bool:
        return super().available and self.line.dnd is not None

    @property
    def is_on(self) -> bool | None:
        return self.line.dnd if self.line else None


class TinCanRecentCallSensor(TinCanCallEntity, BinarySensorEntity):
    """On for `ring_window` seconds after the newest call ends or is missed.

    Stays off while a call is still ringing or in progress (see Call State),
    so it never reports a ringing call as missed.
    """

    _attr_icon = "mdi:phone-ring"

    def __init__(self, coordinator, subscription_id: str) -> None:
        super().__init__(coordinator, subscription_id, "recent_call")
        self._unsub_expiry = None

    @property
    def is_on(self) -> bool:
        active, _ = self.coordinator.recent_call_active(self.call)
        return active

    @property
    def entity_picture(self) -> str | None:
        return self.call.avatar if self.call else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        _, since = self.coordinator.recent_call_active(self.call)
        attrs = self.call.attributes() if self.call else {}
        attrs["seconds_since_end"] = since
        attrs["window_seconds"] = int(self.coordinator.ring_window.total_seconds())
        return attrs

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._schedule_expiry()

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_expiry()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._schedule_expiry()
        super()._handle_coordinator_update()

    def _cancel_expiry(self) -> None:
        if self._unsub_expiry is not None:
            self._unsub_expiry()
            self._unsub_expiry = None

    @callback
    def _schedule_expiry(self) -> None:
        """Flip back to off exactly when the window closes, not on the next poll."""
        self._cancel_expiry()
        if not self.is_on:
            return
        expiry = self.coordinator.recent_call_expiry(self.call)
        if expiry is None:
            return
        self._unsub_expiry = async_track_point_in_utc_time(
            self.hass, self._expire, expiry
        )

    @callback
    def _expire(self, _now: datetime) -> None:
        self._unsub_expiry = None
        self.async_write_ha_state()
