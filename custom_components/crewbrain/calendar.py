"""The jobs, and the absences, as calendars."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import NO_END, Absence, CrewBrainError, CrewBrainForbiddenError, Job
from .const import DOMAIN
from .coordinator import AbsenceCoordinator, JobsCoordinator
from .entity import CrewBrainEntity

if TYPE_CHECKING:
    from . import CrewBrainConfigEntry

#: Words in event descriptions, by Home Assistant's language.
_LABELS = {
    "en": {
        "client": "Client", "project": "Project", "role": "Role",
        "activity": "Activity", "crew": "Crew", "remark": "Remark",
        "notes": "Notes", "absence": "Absence",
        "requested": "Requested, not approved yet",
    },
    "de": {
        "client": "Kunde", "project": "Projekt", "role": "Rolle",
        "activity": "Tätigkeit", "crew": "Team", "remark": "Bemerkung",
        "notes": "Notizen", "absence": "Abwesenheit",
        "requested": "Beantragt, noch nicht genehmigt",
    },
}
#: Local times an absence ends at when it lasts whole days.
_DAY_END = (time(0), time(23, 59), time(23, 59, 59))


async def async_setup_entry(hass: HomeAssistant, entry: CrewBrainConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    data = entry.runtime_data
    entities: list[CalendarEntity] = [Jobs(data.jobs, entry, "jobs")]
    if data.absences.offers("absences"):
        entities.append(Absences(data.absences, entry, "absences"))
    async_add_entities(entities)


def _labels(hass: HomeAssistant) -> dict[str, str]:
    return _LABELS.get(hass.config.language.split("-")[0], _LABELS["en"])


def _event(job: Job, labels: dict[str, str]) -> CalendarEvent:
    lines = [
        f"{job.number}" if job.number else "",
        f"{labels['client']}: {job.client}" if job.client else "",
        f"{labels['project']}: {job.project}" if job.project else "",
        f"{labels['role']}: {job.role}" if job.role else "",
        f"{labels['activity']}: {job.activity}" if job.activity else "",
        f"{labels['crew']}: {', '.join(job.crew)}" if job.crew else "",
        f"{labels['remark']}: {job.remark}" if job.remark else "",
        f"{labels['notes']}: {job.notes}" if job.notes else "",
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


def _absence_event(absence: Absence, labels: dict[str, str]) -> CalendarEvent:
    """Whole days as an all-day event, anything else with its times."""
    start, end = dt_util.as_local(absence.start), dt_util.as_local(absence.end)
    lines = [absence.reason, "" if absence.approved else labels["requested"]]
    details = {
        "summary": absence.kind or labels["absence"],
        "description": "\n".join(line for line in lines if line) or None,
        "uid": f"crewbrain-absence-{absence.id}",
    }
    if start.time() == time(0) and end.time().replace(microsecond=0) in _DAY_END:
        last = end.date() - timedelta(days=1) if end.time() == time(0) else end.date()
        return CalendarEvent(start=start.date(),
                             end=max(last, start.date()) + timedelta(days=1), **details)
    return CalendarEvent(start=start, end=end if end > start else start + NO_END, **details)


def _within(events: list[CalendarEvent], start: datetime,
            end: datetime) -> list[CalendarEvent]:
    return [e for e in events
            if e.end_datetime_local > start and e.start_datetime_local < end]


class Jobs(CrewBrainEntity[JobsCoordinator], CalendarEntity):
    """The jobs this employee is on. Other people's jobs are left out."""

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.utcnow()
        for job in self.coordinator.data:
            if job.until > now:
                return _event(job, _labels(self.hass))
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
        return _within([_event(job, _labels(hass)) for job in jobs], start_date, end_date)


class Absences(CrewBrainEntity[AbsenceCoordinator], CalendarEntity):
    """Vacation, sick and other days off. On while one lasts."""

    @property
    def _absences(self) -> list[Absence] | None:
        data = self.coordinator.data
        return data.absences if data else None

    @property
    def available(self) -> bool:
        return super().available and self._absences is not None

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.utcnow()
        for absence in self._absences or []:
            if absence.end > now:
                return _absence_event(absence, _labels(self.hass))
        return None

    async def async_get_events(self, hass: HomeAssistant, start_date: datetime,
                               end_date: datetime) -> list[CalendarEvent]:
        coordinator = self.coordinator
        inside = (coordinator.window_start is not None
                  and coordinator.window_end is not None
                  and start_date >= coordinator.window_start
                  and end_date <= coordinator.window_end)
        if inside:
            absences = self._absences or []
        else:
            try:
                absences = await coordinator.api.my_absences(start_date, end_date)
            except CrewBrainForbiddenError:
                absences = []
            except CrewBrainError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="api_error",
                    translation_placeholders={"error": str(err)}) from err
        return _within([_absence_event(a, _labels(hass)) for a in absences],
                       start_date, end_date)
