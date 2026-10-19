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
#: The clocked-in-since state while not clocked in; translated for display.
NOT_CLOCKED_IN = "not_clocked_in"


async def async_setup_entry(hass: HomeAssistant, entry: CrewBrainConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    data = entry.runtime_data
    async_add_entities([
        ClockedInSince(data.clock, entry, "clocked_in_since"),
        WorkedToday(data.clock, entry, "worked_today"),
        NextJob(data.jobs, entry, "next_job"),
    ])


class ClockedInSince(CrewBrainEntity[ClockCoordinator], SensorEntity):
    """When the running record started, as text a person reads.

    Text rather than a timestamp, because a timestamp sensor shows "unknown"
    while nobody is clocked in. The `since` attribute keeps the moment for
    automations.
    """

    @property
    def native_value(self) -> str | None:
        state = self.coordinator.data
        if not state.clocked_in:
            return NOT_CLOCKED_IN
        if state.since is None:  # several records open: no single start
            return None
        since = dt_util.as_local(state.since)
        today = since.date() == dt_util.now().date()
        return since.strftime("%H:%M" if today else "%Y-%m-%d %H:%M")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        since = self.coordinator.data.since
        return {"since": since.isoformat() if since else None}


class WorkedToday(CrewBrainEntity[ClockCoordinator], SensorEntity):
    """Time booked today, a running record counted up to the last poll.

    Computed from each record's timestamps, not from CrewBrain's `Duration`.
    A record that spans midnight counts only its part within today.

    In hours, because the frontend shows hours as hours and minutes. It
    takes the minutes as floor((hours - whole hours) * 60), and an exact
    7 h 35 min in floating point comes out as 34.99... and shows a minute
    short. Half a second more is never a minute more, and fixes that.
    """

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float:
        minutes = self.coordinator.data.worked_today
        return round(minutes / 60 + 1 / 7200, 4) if minutes else 0


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
