"""Requesting time off through the absences calendar, and taking it back.

An event added to the calendar is a request: its summary names the type,
its description is the reason. Deleting a request that is not approved yet
withdraws it. Approving is the company's business, never this code's.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.calendar.const import (
    EVENT_DESCRIPTION,
    EVENT_END,
    EVENT_RRULE,
    EVENT_START,
    EVENT_SUMMARY,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from .api import Absence, CrewBrainAuthError, CrewBrainError, CrewBrainStateError
from .const import DOMAIN, OPT_STAND_IN

if TYPE_CHECKING:
    from . import CrewBrainConfigEntry

#: The stand-in option for nobody.
NO_STAND_IN = "none"
_UID = "crewbrain-absence-"


def absence_uid(absence_id: int) -> str:
    return f"{_UID}{absence_id}"


def stand_in_choices(entry: CrewBrainConfigEntry) -> dict[int, str]:
    """Who can stand in: the staff list, or else the colleagues from the jobs."""
    data = entry.runtime_data
    return data.colleagues or data.api.crew_seen


def _stand_in(entry: CrewBrainConfigEntry) -> int | None:
    """The one chosen in the options, or the one CrewBrain has on file."""
    chosen = entry.options.get(OPT_STAND_IN)
    if chosen == NO_STAND_IN:
        return None
    return int(chosen) if chosen else entry.runtime_data.default_stand_in


def _day_end(day: date, absences: list[Absence]) -> datetime:
    """The end of a requested last day, the way this company's whole days end.

    At midnight or a second before: whichever the newest whole-day absence
    uses, so a request reads like one made in CrewBrain's own app.
    """
    midnight = dt_util.start_of_local_day(day + timedelta(days=1))
    whole = [a for a in absences if dt_util.as_local(a.start).time() == time(0)]
    if whole and dt_util.as_local(max(whole, key=lambda a: a.start).end).time() == time(0):
        return midnight
    return midnight - timedelta(seconds=1)


def _placeholders(err: CrewBrainStateError) -> dict[str, str]:
    absence = err.absence
    if absence is None:
        return {}
    return {"kind": absence.kind or "?",
            "day": dt_util.as_local(absence.start).date().isoformat()}


async def _run(entry: CrewBrainConfigEntry, call: Any) -> Any:
    """Map CrewBrain's answers to messages, and show the absences as they are now."""
    try:
        return await call
    except CrewBrainStateError as err:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key=err.key,
                                     translation_placeholders=_placeholders(err)) from err
    except CrewBrainAuthError as err:
        entry.async_start_reauth(entry.runtime_data.hass)
        raise HomeAssistantError(translation_domain=DOMAIN,
                                 translation_key="auth_failed") from err
    except CrewBrainError as err:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="api_error",
                                 translation_placeholders={"error": str(err)}) from err
    finally:
        await entry.runtime_data.absences.async_refresh()


def _refuse(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(translation_domain=DOMAIN, translation_key=key,
                                  translation_placeholders=placeholders or None)


async def async_request(entry: CrewBrainConfigEntry, event: dict[str, Any]) -> None:
    """A calendar event added to the absences calendar, as a time-off request."""
    data = entry.runtime_data
    first, end = event[EVENT_START], event[EVENT_END]
    if event.get(EVENT_RRULE):
        raise _refuse("time_off_no_repeat")
    if isinstance(first, datetime) or isinstance(end, datetime):
        raise _refuse("time_off_whole_days")
    types = data.api.time_off_types
    asked = str(event.get(EVENT_SUMMARY) or "").strip()
    category = next((i for i, t in types.items() if t.casefold() == asked.casefold()), None)
    if category is None:
        raise _refuse("unknown_time_off_type", type=asked,
                      types=", ".join(types.values()) or "-")
    # A calendar's all-day end is the day after the last one.
    last = end - timedelta(days=1) if end > first else first
    known = (data.absences.data.absences if data.absences.data else None) or []
    await _run(entry, data.api.request_time_off(
        dt_util.start_of_local_day(first), _day_end(last, known), category,
        str(event.get(EVENT_DESCRIPTION) or "").strip(), _stand_in(entry)))


async def async_withdraw(entry: CrewBrainConfigEntry, uid: str) -> None:
    """A request deleted from the calendar, withdrawn if it is not approved yet."""
    data = entry.runtime_data
    try:
        absence_id = int(uid.removeprefix(_UID))
    except ValueError:
        raise _refuse("absence_gone") from None
    known = (data.absences.data.absences if data.absences.data else None) or []
    shown = next((a for a in known if a.id == absence_id), None)
    if shown is not None:
        since, until = shown.start, shown.end
    else:
        now = dt_util.utcnow()
        since, until = now - timedelta(days=366), now + timedelta(days=3 * 366)
    await _run(entry, data.api.withdraw_time_off(absence_id, since, until))
