"""CrewBrain: punch clock and jobs for one CrewBrain account."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_API_TOKEN, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import CrewBrainApi, CrewBrainAuthError, CrewBrainError, default_category
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_NOTE,
    ATTR_TIME,
    CONF_BASE_URL,
    CONF_EMPLOYEE_ID,
    DOMAIN,
    SERVICE_CLOCK_IN,
    SERVICE_CLOCK_OUT,
)
from .coordinator import AbsenceCoordinator, ClockCoordinator, JobsCoordinator
from .punch import async_clock_in, async_clock_out

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.CALENDAR, Platform.SENSOR, Platform.SWITCH]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class CrewBrainData:
    """What one config entry holds while it is loaded."""

    hass: HomeAssistant
    api: CrewBrainApi
    clock: ClockCoordinator
    jobs: JobsCoordinator
    absences: AbsenceCoordinator
    #: The company's active time categories, read once per load.
    categories: list[dict[str, Any]] = field(default_factory=list)
    #: Where a clock-in books when the options name no category.
    default_category: int | None = None


type CrewBrainConfigEntry = ConfigEntry[CrewBrainData]

_CLOCK_IN_SCHEMA = vol.Schema({
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Optional(ATTR_TIME): cv.datetime,
    vol.Optional(ATTR_NOTE): cv.string,
})
_CLOCK_OUT_SCHEMA = vol.Schema({
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Optional(ATTR_TIME): cv.datetime,
})


def _entry_for(hass: HomeAssistant, call: ServiceCall) -> CrewBrainConfigEntry:
    """The entry an action is meant for. Optional when there is only one."""
    loaded = [e for e in hass.config_entries.async_entries(DOMAIN)
              if e.state is ConfigEntryState.LOADED]
    wanted = call.data.get(ATTR_CONFIG_ENTRY_ID)
    if wanted:
        loaded = [e for e in loaded if e.entry_id == wanted]
    if len(loaded) != 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_entry" if not loaded else "choose_entry",
        )
    return loaded[0]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions once, for every account."""

    async def clock_in(call: ServiceCall) -> ServiceResponse:
        entry = _entry_for(hass, call)
        return await async_clock_in(entry, call.data.get(ATTR_TIME),
                                    call.data.get(ATTR_NOTE))

    async def clock_out(call: ServiceCall) -> ServiceResponse:
        entry = _entry_for(hass, call)
        return await async_clock_out(entry, call.data.get(ATTR_TIME))

    hass.services.async_register(DOMAIN, SERVICE_CLOCK_IN, clock_in,
                                 schema=_CLOCK_IN_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    hass.services.async_register(DOMAIN, SERVICE_CLOCK_OUT, clock_out,
                                 schema=_CLOCK_OUT_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CrewBrainConfigEntry) -> bool:
    api = CrewBrainApi(
        async_get_clientsession(hass),
        entry.data[CONF_BASE_URL],
        entry.data[CONF_API_TOKEN],
        employee_id=entry.data[CONF_EMPLOYEE_ID],
    )
    clock = ClockCoordinator(hass, entry, api)
    jobs = JobsCoordinator(hass, entry, api)
    await clock.async_config_entry_first_refresh()
    await jobs.async_config_entry_first_refresh()
    absences = AbsenceCoordinator(hass, entry, api)
    # Optional: a failure here never keeps the punch clock from loading.
    await absences.async_refresh()
    try:
        categories = await api.time_categories()
    except CrewBrainAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except CrewBrainError as err:
        # Everything but clocking in works without them, and that still
        # works with a category chosen earlier.
        _LOGGER.warning("CrewBrain: time categories not readable: %s", err)
        categories = []
    entry.runtime_data = CrewBrainData(
        hass=hass, api=api, clock=clock, jobs=jobs, absences=absences,
        categories=categories,
        default_category=default_category(categories))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CrewBrainConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
