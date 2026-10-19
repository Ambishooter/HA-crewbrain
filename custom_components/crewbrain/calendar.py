"""The jobs as a calendar."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import CrewBrainError, Job
from .const import DOMAIN
from .coordinator import JobsCoordinator
from .entity import CrewBrainEntity

if TYPE_CHECKING:
    from . import CrewBrainConfigEntry

#: Field names in an event's description, by Home Assistant's language.
_LABELS = {"de": ("Kunde", "Projekt")}
_DEFAULT_LABELS = ("Client", "Project")


async def async_setup_entry(hass: HomeAssistant, entry: CrewBrainConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([Jobs(entry.runtime_data.jobs, entry, "jobs")])


def _event(job: Job, labels: tuple[str, str]) -> CalendarEvent:
    client, project = labels
    lines = [
        f"{job.number}" if job.number else "",
        f"{client}: {job.client}" if job.client else "",
        f"{project}: {job.project}" if job.project else "",
        job.description,
    ]
    return CalendarEvent(
        start=dt_util.as_local(job.start),
        end=dt_util.as_local(job.until),
        summary=job.title or (job.number or "Job"),
        description="\n".join(line for line in lines if line) or None,
        location=job.place,
        uid=f"crewbrain-{job.id}",
    )


class Jobs(CrewBrainEntity[JobsCoordinator], CalendarEntity):
    """The jobs this employee is on. Other people's jobs are left out."""

    @property
    def _labels(self) -> tuple[str, str]:
        return _LABELS.get(self.hass.config.language.split("-")[0], _DEFAULT_LABELS)

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.utcnow()
        for job in self.coordinator.data:
            if job.until > now:
                return _event(job, self._labels)
        return None

    async def async_get_events(self, hass: HomeAssistant, start_date: datetime,
                               end_date: datetime) -> list[CalendarEvent]:
        coordinator = self.coordinator
        inside = (coordinator.window_start is not None
                  and coordinator.window_end is not None
                  and start_date >= coordinator.window_start
                  and end_date <= coordinator.window_end)
        if inside:
            jobs = coordinator.data
        else:
            # Outside the polled window (another month in the calendar view):
            # read it now. A job that started up to a week earlier and is
            # still running is included.
            try:
                jobs = await coordinator.api.my_jobs(
                    start_date - timedelta(days=7), end_date, ending_after=start_date)
            except CrewBrainError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="api_error",
                    translation_placeholders={"error": str(err)}) from err
        events = [_event(job, self._labels) for job in jobs]
        return [e for e in events if e.end > start_date and e.start < end_date]
