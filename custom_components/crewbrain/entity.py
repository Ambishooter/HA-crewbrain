"""The device every CrewBrain entity hangs off."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import CONF_BASE_URL, DOMAIN

if TYPE_CHECKING:
    from . import CrewBrainConfigEntry


class CrewBrainEntity[_C: DataUpdateCoordinator](CoordinatorEntity[_C]):
    """One account is one device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: _C, entry: CrewBrainConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            name=entry.title,
            manufacturer="CrewBrain",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=entry.data[CONF_BASE_URL],
        )
