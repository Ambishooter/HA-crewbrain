"""The punch clock as a switch: on clocks in, off clocks out."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ClockCoordinator
from .entity import CrewBrainEntity
from .punch import async_clock_in, async_clock_out

if TYPE_CHECKING:
    from . import CrewBrainConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: CrewBrainConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([PunchClock(entry.runtime_data.clock, entry, "punch_clock")])


class PunchClock(CrewBrainEntity[ClockCoordinator], SwitchEntity):
    """Shows CrewBrain's state, never a state of its own.

    Each switch reads CrewBrain first and refuses when the state shown was
    out of date, so a stale dashboard cannot create a second record.
    """

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.clocked_in

    async def async_turn_on(self, **kwargs: Any) -> None:
        await async_clock_in(self.entry)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await async_clock_out(self.entry)
