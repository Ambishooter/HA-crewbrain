"""The activity a clock-in writes, set in Home Assistant and kept over restarts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import ACTIVITY_MAX
from .coordinator import ClockCoordinator
from .entity import CrewBrainEntity

if TYPE_CHECKING:
    from . import CrewBrainConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: CrewBrainConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([Activity(entry.runtime_data.clock, entry, "activity")])


class Activity(CrewBrainEntity[ClockCoordinator], TextEntity, RestoreEntity):
    """What the switch writes into a clock-in's activity. Empty writes none.

    An action's `note` replaces it for that one clock-in, so an automation
    can say where a clock-in came from without changing this.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = TextMode.TEXT
    _attr_native_min = 0
    _attr_native_max = ACTIVITY_MAX

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            self.entry.runtime_data.activity = last.state

    @property
    def native_value(self) -> str:
        return self.entry.runtime_data.activity

    @property
    def available(self) -> bool:
        # A setting, not a reading: usable while CrewBrain is not reachable.
        return True

    async def async_set_value(self, value: str) -> None:
        self.entry.runtime_data.activity = value.strip()
        self.async_write_ha_state()
