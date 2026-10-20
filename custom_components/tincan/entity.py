"""Base entity helpers for Tin Can."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import TinCanCallCoordinator, TinCanCoordinator, TinCanLine


def line_device_info(line: TinCanLine) -> DeviceInfo:
    """One HA device per Tin Can line, keyed on the subscription id."""
    device = line.device or {}
    return DeviceInfo(
        identifiers={(DOMAIN, line.subscription_id)},
        name=line.name,
        manufacturer=MANUFACTURER,
        model=device.get("device_type") or line.subscription.get("primary_device_type") or "TINCAN",
        serial_number=device.get("mac_address"),
        configuration_url="https://tincan.kids",
    )


class TinCanEntity(CoordinatorEntity[TinCanCoordinator]):
    """Entity backed by the slow (account) coordinator."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TinCanCoordinator, subscription_id: str, key: str) -> None:
        super().__init__(coordinator)
        self.subscription_id = subscription_id
        self._attr_unique_id = f"{subscription_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = line_device_info(coordinator.data[subscription_id])

    @property
    def line(self) -> TinCanLine | None:
        """Current data for this line, if the account still has it."""
        return (self.coordinator.data or {}).get(self.subscription_id)

    @property
    def available(self) -> bool:
        return super().available and self.line is not None


class TinCanCallEntity(CoordinatorEntity[TinCanCallCoordinator]):
    """Entity backed by the fast (call log) coordinator."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: TinCanCallCoordinator, subscription_id: str, key: str
    ) -> None:
        super().__init__(coordinator)
        self.subscription_id = subscription_id
        self._attr_unique_id = f"{subscription_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = line_device_info(coordinator.account.data[subscription_id])

    @property
    def line(self) -> TinCanLine | None:
        return (self.coordinator.account.data or {}).get(self.subscription_id)

    @property
    def call(self):
        """Latest enriched call record for this line (CallInfo or None)."""
        return (self.coordinator.data or {}).get(self.subscription_id)

    @property
    def available(self) -> bool:
        return super().available and self.line is not None
