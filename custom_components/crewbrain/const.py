"""Constants for the CrewBrain integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "crewbrain"

CONF_BASE_URL = "base_url"
CONF_EMPLOYEE_ID = "employee_id"

OPT_DAYS_AHEAD = "days_ahead"
OPT_ACTIVITY = "activity"
OPT_CATEGORY = "category"
DEFAULT_DAYS_AHEAD = 14
DEFAULT_ACTIVITY = "Home Assistant"

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
#: Jobs per request. More than this in one window is logged, not hidden.
EVENTS_LIMIT = 200

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_TIME = "time"
ATTR_NOTE = "note"

SERVICE_CLOCK_IN = "clock_in"
SERVICE_CLOCK_OUT = "clock_out"
