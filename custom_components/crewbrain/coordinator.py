"""Polling: the clocked-in state every minute, jobs every half hour,
absences every hour."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Awaitable
from datetime import date, datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    Absence,
    CrewBrainApi,
    CrewBrainAuthError,
    CrewBrainError,
    CrewBrainForbiddenError,
    Job,
    parse_stamp,
)
from .const import (
    ABSENCE_INTERVAL,
    CLOCK_INTERVAL,
    DEFAULT_DAYS_AHEAD,
    DOMAIN,
    JOBS_INTERVAL,
    JOBS_LOOKBACK,
    OPT_DAYS_AHEAD,
)

if TYPE_CHECKING:
    from . import CrewBrainConfigEntry

_LOGGER = logging.getLogger(__name__)


@dataclass
class ClockState:
    """What the punch clock shows."""

    open_records: list[dict[str, Any]] = field(default_factory=list)
    worked_today: int = 0

    @property
    def record(self) -> dict[str, Any] | None:
        """The running record, if exactly one is running."""
        return self.open_records[0] if len(self.open_records) == 1 else None

    @property
    def clocked_in(self) -> bool:
        return bool(self.open_records)

    @property
    def since(self) -> datetime | None:
        record = self.record
        return parse_stamp(record.get("DateFrom")) if record else None


class ClockCoordinator(DataUpdateCoordinator[ClockState]):
    """Whether a record is running, and the minutes booked today."""

    config_entry: CrewBrainConfigEntry

    def __init__(self, hass: HomeAssistant, entry: CrewBrainConfigEntry,
                 api: CrewBrainApi) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry,
                         name=f"{DOMAIN} clock", update_interval=CLOCK_INTERVAL)
        self.api = api

    async def _async_update_data(self) -> ClockState:
        now = dt_util.utcnow()
        today = dt_util.start_of_local_day()
        tomorrow = dt_util.start_of_local_day(today.date() + timedelta(days=1))
        try:
            open_records = await self.api.open_stamps()
            # From yesterday: a shift that ran past midnight counts its part
            # after midnight today.
            entries = await self.api.entries(today - timedelta(days=1), tomorrow)
        except CrewBrainAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except CrewBrainError as err:
            raise UpdateFailed(str(err)) from err
        # A running record may have started before yesterday.
        records = {r.get("ID", id(r)): r for r in [*entries, *open_records]}
        seconds = sum(_seconds_within(r, today, tomorrow, now) for r in records.values())
        return ClockState(open_records=open_records, worked_today=int(seconds // 60))


def _seconds_within(record: dict[str, Any], start: datetime, end: datetime,
                    now: datetime) -> float:
    """How much of a record lies between start and end. A running one runs to now."""
    began = parse_stamp(record.get("DateFrom"))
    if began is None:
        return 0.0
    ended = parse_stamp(record.get("DateUntil")) or now
    return max(0.0, (min(ended, end) - max(began, start)).total_seconds())


class JobsCoordinator(DataUpdateCoordinator[list[Job]]):
    """The jobs this employee is on, from a week back to `days ahead`."""

    config_entry: CrewBrainConfigEntry

    def __init__(self, hass: HomeAssistant, entry: CrewBrainConfigEntry,
                 api: CrewBrainApi) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry,
                         name=f"{DOMAIN} jobs", update_interval=JOBS_INTERVAL)
        self.api = api
        self.window_start: datetime | None = None
        self.window_end: datetime | None = None

    @property
    def days_ahead(self) -> int:
        return int(self.config_entry.options.get(OPT_DAYS_AHEAD, DEFAULT_DAYS_AHEAD))

    async def _async_update_data(self) -> list[Job]:
        today = dt_util.start_of_local_day()
        start, end = today - JOBS_LOOKBACK, today + timedelta(days=self.days_ahead)
        try:
            jobs = await self.api.my_jobs(start, end, ending_after=today)
        except CrewBrainAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except CrewBrainError as err:
            raise UpdateFailed(str(err)) from err
        # Jobs that ended before today were never read, so the window that
        # is complete starts today.
        self.window_start, self.window_end = today, end
        return jobs


@dataclass
class AbsenceState:
    """The vacation account and the absences.

    None where the company does not let this login read them: then there is
    nothing to show, which is not an error.
    """

    account: dict[str, Any] | None
    absences: list[Absence] | None


class AbsenceCoordinator(DataUpdateCoordinator[AbsenceState]):
    """Vacation and sick days, from the start of this year to the end of next."""

    config_entry: CrewBrainConfigEntry

    def __init__(self, hass: HomeAssistant, entry: CrewBrainConfigEntry,
                 api: CrewBrainApi) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry,
                         name=f"{DOMAIN} absences", update_interval=ABSENCE_INTERVAL)
        self.api = api
        self.window_start: datetime | None = None
        self.window_end: datetime | None = None

    def offers(self, part: str) -> bool:
        """Whether to set up the entity for `account` or `absences`.

        Not read yet, because CrewBrain did not answer, counts as yes.
        """
        return not self.last_update_success or getattr(self.data, part) is not None

    async def _async_update_data(self) -> AbsenceState:
        year = dt_util.now().year
        start = dt_util.start_of_local_day(date(year, 1, 1))
        end = dt_util.start_of_local_day(date(year + 2, 1, 1))
        try:
            account = await _unless_forbidden(self.api.vacation_account())
            absences = await _unless_forbidden(self.api.my_absences(start, end))
        except CrewBrainAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except CrewBrainError as err:
            raise UpdateFailed(str(err)) from err
        self.window_start, self.window_end = start, end
        return AbsenceState(account=account, absences=absences)


async def _unless_forbidden[T](call: Awaitable[T]) -> T | None:
    """What a read returns, or None when the company does not allow it."""
    try:
        return await call
    except CrewBrainForbiddenError:
        return None
