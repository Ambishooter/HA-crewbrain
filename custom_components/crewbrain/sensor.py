"""Since when, how long today, and the next job."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import Job
from .coordinator import ClockCoordinator, JobsCoordinator
from .entity import CrewBrainEntity

if TYPE_CHECKING:
    from . import CrewBrainConfigEntry

#: Jobs listed in the next-job sensor's `upcoming` attribute.
UPCOMING = 20


async def async_setup_entry(hass: HomeAssistant, entry: CrewBrainConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    data = entry.runtime_data
    async_add_entities([
        ClockedInSince(data.clock, entry, "clocked_in_since"),
        WorkedToday(data.clock, entry, "worked_today"),
        NextJob(data.jobs, entry, "next_job"),
    ])


class ClockedInSince(CrewBrainEntity[ClockCoordinator], SensorEntity):
    """When the running record started. Unknown when none is running."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.data.since


class WorkedToday(CrewBrainEntity[ClockCoordinator], SensorEntity):
    """Minutes booked today, a running record counted up to the last poll.

    Computed from each record's timestamps, not from CrewBrain's `Duration`.
    A record that spans midnight counts only its part within today.
    """

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> int:
        return self.coordinator.data.worked_today


class NextJob(CrewBrainEntity[JobsCoordinator], SensorEntity):
    """The start of the next job that is not over yet."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _unrecorded_attributes = frozenset({"description", "upcoming"})

    def _coming(self) -> list[Job]:
        now = dt_util.utcnow()
        return [j for j in self.coordinator.data if j.until > now]

    @property
    def native_value(self) -> datetime | None:
        coming = self._coming()
        return coming[0].start if coming else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coming = self._coming()
        attributes: dict[str, Any] = {
            "upcoming": [j.as_dict() for j in coming[:UPCOMING]],
        }
        if coming:
            job = coming[0]
            attributes.update(
                job.as_dict(),
                place=job.place,
                description=job.description,
                status=job.status,
            )
        return attributes
