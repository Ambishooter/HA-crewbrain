"""Constants for the CrewBrain integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "crewbrain"

CONF_BASE_URL = "base_url"
CONF_EMPLOYEE_ID = "employee_id"

OPT_DAYS_AHEAD = "days_ahead"
OPT_ACTIVITY = "activity"
OPT_CATEGORY = "category"
OPT_STAND_IN = "stand_in"
DEFAULT_DAYS_AHEAD = 14

#: The clocked-in state is two small requests. Jobs cost one request per job
#: in the window, so they are read far less often.
CLOCK_INTERVAL = timedelta(seconds=60)
JOBS_INTERVAL = timedelta(minutes=30)
#: The vacation account and the absences change rarely.
ABSENCE_INTERVAL = timedelta(hours=1)
#: How far back the job list reaches, so a job that started earlier and is
#: still running is not lost. Jobs already over are dropped before their
#: crew is read.
JOBS_LOOKBACK = timedelta(days=7)

#: CrewBrain's timestamp format. Everything it stores and returns is UTC.
STAMP = "%Y%m%dT%H%M%SZ"
CAPTURE_MODE = "API"
#: A booking with no job attached.
INDEPENDENT = "INDEPENDENTHOURS"
#: A booking on a job. Its `AssignmentID` is the employee's place in the
#: job's crew, from `/v2/events/{id}/workinghourEmployees`, not the job's ID.
ON_JOB = "JOBEMPLOYEE"
#: CrewBrain cuts `Activity` at this length.
ACTIVITY_MAX = 100

#: What the job choice can be besides one job's ID.
JOB_AUTO = "auto"
JOB_NONE = "none"
#: How early before a job "automatic" already books onto it: arriving
#: early is common, clocking in an hour early is not.
AUTO_LEAD = timedelta(minutes=60)
#: The activity text earlier versions wrote by default. Taken as "no text".
LEGACY_ACTIVITY = "Home Assistant"
#: Jobs per request. More than this in one window is logged, not hidden.
EVENTS_LIMIT = 200

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_TIME = "time"
ATTR_NOTE = "note"
ATTR_JOB = "job"

SERVICE_CLOCK_IN = "clock_in"
SERVICE_CLOCK_OUT = "clock_out"
