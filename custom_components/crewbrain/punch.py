"""Clocking in and out. Shared by the switch and the actions."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from .api import (
    CrewBrainAuthError,
    CrewBrainError,
    CrewBrainStateError,
    Job,
    duration_minutes,
    parse_stamp,
)
from .const import AUTO_LEAD, DOMAIN, JOB_AUTO, JOB_NONE, OPT_CATEGORY

if TYPE_CHECKING:
    from . import CrewBrainConfigEntry

_LOGGER = logging.getLogger(__name__)


def _local_time(stamp: Any) -> str:
    moment = parse_stamp(stamp)
    return dt_util.as_local(moment).strftime("%H:%M") if moment else "?"


def _result(record: dict[str, Any]) -> dict[str, Any]:
    began, ended = parse_stamp(record.get("DateFrom")), parse_stamp(record.get("DateUntil"))
    return {
        "record_id": record.get("ID"),
        "start": dt_util.as_local(began).isoformat() if began else None,
        "end": dt_util.as_local(ended).isoformat() if ended else None,
        "minutes": duration_minutes(record),
    }


def upcoming_jobs(entry: CrewBrainConfigEntry, now: datetime) -> list[Job]:
    """The jobs that can still be clocked in on: not over by `now`."""
    return [j for j in entry.runtime_data.jobs.data or [] if j.until > now]


def automatic_job(jobs: list[Job], now: datetime, *, warn: bool = True) -> Job | None:
    """The job "automatic" books onto at `now`, or None.

    One that is running, else one that starts within `AUTO_LEAD`. When two
    qualify alike, none is picked: a wrong job is worse than no job.
    """
    running = [j for j in jobs if j.start <= now < j.until]
    soon = [j for j in jobs if now < j.start <= now + AUTO_LEAD]
    for pool in (running, soon):
        if len(pool) == 1:
            return pool[0]
        if pool and warn:
            _LOGGER.warning("CrewBrain: %s jobs qualify for an automatic clock-in (%s); "
                            "booked without a job", len(pool),
                            ", ".join(j.title for j in pool))
        if pool:
            return None
    return None


def resolve_job(entry: CrewBrainConfigEntry, choice: str,
                now: datetime) -> Job | None:
    """What a job choice means at `now`: `auto`, `none` or a job's ID."""
    jobs = upcoming_jobs(entry, now)
    if choice == JOB_NONE:
        return None
    if choice == JOB_AUTO:
        return automatic_job(jobs, now)
    found = [j for j in jobs if str(j.id) == str(choice).strip()]
    if not found:
        raise ServiceValidationError(translation_domain=DOMAIN,
                                     translation_key="unknown_job",
                                     translation_placeholders={"job": str(choice)})
    return found[0]


async def _run(entry: CrewBrainConfigEntry, call: Any) -> dict[str, Any]:
    try:
        return await call
    except CrewBrainStateError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=err.key,
            translation_placeholders={
                "since": _local_time(err.record.get("DateFrom")),
                "count": str(err.count),
            },
        ) from err
    except CrewBrainAuthError as err:
        entry.async_start_reauth(entry.runtime_data.hass)
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="auth_failed") from err
    except CrewBrainError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="api_error",
            translation_placeholders={"error": str(err)},
        ) from err
    finally:
        # Also after a refusal: it says the state shown was out of date.
        await entry.runtime_data.clock.async_refresh()


async def async_clock_in(entry: CrewBrainConfigEntry, when: datetime | None = None,
                         note: str | None = None,
                         job: str | None = None) -> dict[str, Any]:
    """Clock in. `when` without a time zone is read as Home Assistant's.

    `note` and `job` are for this one clock-in; left out, the activity and
    the job chosen in Home Assistant apply.
    """
    data = entry.runtime_data
    activity = note if note is not None else data.activity
    category = entry.options.get(OPT_CATEGORY) or data.default_category
    if not category:
        raise ServiceValidationError(translation_domain=DOMAIN,
                                     translation_key="no_category")
    at = dt_util.as_utc(when) if when else None
    moment = at or dt_util.utcnow()
    if job is not None:
        chosen = resolve_job(entry, job, moment)
    else:
        try:
            chosen = resolve_job(entry, data.job_choice, moment)
        except ServiceValidationError:
            # The job chosen in Home Assistant is over: the choice falls
            # back to automatic, as the select does at its next refresh.
            chosen = resolve_job(entry, JOB_AUTO, moment)
    record = await _run(entry, data.api.clock_in(
        at, activity, int(category), chosen.id if chosen else None))
    return {**_result(record), "job_id": chosen.id if chosen else None,
            "job": chosen.title if chosen else None}


async def async_clock_out(entry: CrewBrainConfigEntry,
                          when: datetime | None = None) -> dict[str, Any]:
    """Clock out. `when` without a time zone is read as Home Assistant's."""
    at = dt_util.as_utc(when) if when else None
    return _result(await _run(entry, entry.runtime_data.api.clock_out(at)))
