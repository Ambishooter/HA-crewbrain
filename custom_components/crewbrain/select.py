"""The job a clock-in books on, chosen in Home Assistant and kept over restarts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.select import SelectEntity
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .api import Job
from .const import DOMAIN, JOB_AUTO, JOB_NONE
from .coordinator import JobsCoordinator
from .entity import CrewBrainEntity
from .punch import automatic_job, upcoming_jobs

if TYPE_CHECKING:
    from . import CrewBrainConfigEntry

ATTR_JOB_ID = "job_id"


async def async_setup_entry(hass: HomeAssistant, entry: CrewBrainConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([JobChoice(entry.runtime_data.jobs, entry, "clock_in_job")])


def job_labels(jobs: list[Job]) -> dict[str, int]:
    """Each job as the select shows it, start and title, mapped to its ID.

    Two jobs alike get their number, or their ID, to keep them apart.
    """
    labels: dict[str, int] = {}
    plain = [f"{dt_util.as_local(j.start).strftime('%d.%m. %H:%M')} {j.title}".strip()
             for j in jobs]
    for job, label in zip(jobs, plain, strict=True):
        if plain.count(label) > 1:
            label = f"{label} ({job.number or job.id})"
        labels[label] = job.id
    return labels


class JobChoice(CrewBrainEntity[JobsCoordinator], SelectEntity, RestoreEntity):
    """`auto`, `none`, or one of the jobs that are not over yet.

    A chosen job that is over, or no longer this employee's, falls back to
    `auto`. An action's `job` replaces the choice for that one clock-in.
    """

    _attr_entity_category = EntityCategory.CONFIG

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            kept = last.attributes.get(ATTR_JOB_ID)
            if last.state in (JOB_AUTO, JOB_NONE):
                self.entry.runtime_data.job_choice = last.state
            elif kept is not None:
                self.entry.runtime_data.job_choice = str(kept)
        self._drop_if_over()

    @property
    def _labels(self) -> dict[str, int]:
        return job_labels(upcoming_jobs(self.entry, dt_util.utcnow()))

    def _drop_if_over(self) -> None:
        choice = self.entry.runtime_data.job_choice
        if choice not in (JOB_AUTO, JOB_NONE) and \
                choice not in {str(i) for i in self._labels.values()}:
            self.entry.runtime_data.job_choice = JOB_AUTO

    @callback
    def _handle_coordinator_update(self) -> None:
        self._drop_if_over()
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        # A setting: `auto` and `none` work while CrewBrain is not reachable.
        return True

    @property
    def options(self) -> list[str]:
        return [JOB_AUTO, JOB_NONE, *self._labels]

    @property
    def current_option(self) -> str:
        choice = self.entry.runtime_data.job_choice
        if choice in (JOB_AUTO, JOB_NONE):
            return choice
        for label, job_id in self._labels.items():
            if str(job_id) == choice:
                return label
        return JOB_AUTO

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        choice = self.entry.runtime_data.job_choice
        now = dt_util.utcnow()
        attributes: dict[str, Any] = {
            ATTR_JOB_ID: int(choice) if choice not in (JOB_AUTO, JOB_NONE) else None}
        if choice == JOB_AUTO:
            # What a clock-in would pick right now. Read with care: it is as
            # of the last time this state was written.
            picked = automatic_job(upcoming_jobs(self.entry, now), now, warn=False)
            attributes["automatic_job"] = picked.title if picked else None
        return attributes

    async def async_select_option(self, option: str) -> None:
        if option in (JOB_AUTO, JOB_NONE):
            self.entry.runtime_data.job_choice = option
        elif option in self._labels:
            self.entry.runtime_data.job_choice = str(self._labels[option])
        else:
            raise ServiceValidationError(translation_domain=DOMAIN,
                                         translation_key="unknown_job",
                                         translation_placeholders={"job": option})
        self.async_write_ha_state()
