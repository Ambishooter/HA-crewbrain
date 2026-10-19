"""Whether a record is running."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ClockCoordinator
from .entity import CrewBrainEntity

if TYPE_CHECKING:
    from . import CrewBrainConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: CrewBrainConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([ClockedIn(entry.runtime_data.clock, entry, "clocked_in")])


class ClockedIn(CrewBrainEntity[ClockCoordinator], BinarySensorEntity):
    """On while a record is running. Shown as clocked in or out."""

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.clocked_in

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.coordinator.data
        record = state.record or {}
        attributes: dict[str, Any] = {
            "record_id": record.get("ID"),
            "activity": record.get("Activity"),
            "open_records": len(state.open_records),
        }
        return attributes
