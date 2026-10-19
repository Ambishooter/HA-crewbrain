"""Clocking in and out. Shared by the switch and the actions."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from .api import CrewBrainAuthError, CrewBrainError, CrewBrainStateError, duration_minutes, parse_stamp
from .const import DEFAULT_ACTIVITY, DOMAIN, OPT_ACTIVITY, OPT_CATEGORY

if TYPE_CHECKING:
    from . import CrewBrainConfigEntry


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


async def _run(entry: CrewBrainConfigEntry, call: Any) -> dict[str, Any]:
    try:
        record = await call
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
    return _result(record)


async def async_clock_in(entry: CrewBrainConfigEntry, when: datetime | None = None,
                         note: str | None = None) -> dict[str, Any]:
    """Clock in. `when` without a time zone is read as Home Assistant's."""
    activity = note or entry.options.get(OPT_ACTIVITY, DEFAULT_ACTIVITY)
    category = entry.options.get(OPT_CATEGORY) or entry.runtime_data.default_category
    if not category:
        raise ServiceValidationError(translation_domain=DOMAIN,
                                     translation_key="no_category")
    at = dt_util.as_utc(when) if when else None
    return await _run(entry, entry.runtime_data.api.clock_in(at, activity, int(category)))


async def async_clock_out(entry: CrewBrainConfigEntry,
                          when: datetime | None = None) -> dict[str, Any]:
    """Clock out. `when` without a time zone is read as Home Assistant's."""
    at = dt_util.as_utc(when) if when else None
    return await _run(entry, entry.runtime_data.api.clock_out(at))
