"""CrewBrain's REST API: the parts a punch clock and a job list need.

What this relies on was checked against a live instance:

- `GET /api/accesstoken` with Basic auth returns the access token,
  `{"Accesstoken": "user:secret"}`, labelled `text/html`. It stays valid
  for 14 days from its last use.
- Authentication is the header `X-API-KEY`, carrying the whole token.
- `<company>.crewbrain.com` answers every path with a 308 to its region host.
- Every timestamp is UTC, written `20261019T060000Z`.
- There is no clock-out endpoint. Clocking out puts a `DateUntil` on the
  open record.
- `Duration` is wrong whenever the minute part is below ten (`0:07` is
  stored as `0:70`), so durations are computed from the timestamps.
- Time categories are each company's own, listed by `/v2/categories/time`.
- `GET /v2/events` answers 400 without a date range, and has no filter for
  "my jobs". The crew of every job is read and matched instead.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import html
import json
import logging
import re
from typing import Any
from urllib.parse import urlencode

import aiohttp
from yarl import URL

from .const import (
    CAPTURE_MODE,
    EVENTS_LIMIT,
    INDEPENDENT,
    STAMP,
)

_LOGGER = logging.getLogger(__name__)

#: The password and the API key go to hosts under this domain and nowhere
#: else. aiohttp would carry a custom header to any host a redirect names,
#: so redirects are followed by hand.
SERVICE_DOMAIN = "crewbrain.com"

#: A job without an end, or with one before its start, still needs a length.
NO_END = timedelta(hours=1)

_REDIRECTS = (301, 302, 307, 308)
_TAG = re.compile(r"<[^>]+>")


class CrewBrainError(Exception):
    """A request failed. The message names the request and the reason."""


class CrewBrainAuthError(CrewBrainError):
    """CrewBrain refused the token, or the username and password."""


class CrewBrainStateError(CrewBrainError):
    """The punch clock is not in a state that allows the action.

    `key` is a translation key under `exceptions` in the translations.
    """

    def __init__(self, key: str, record: dict[str, Any] | None = None,
                 count: int = 0) -> None:
        super().__init__(key)
        self.key = key
        self.record = record or {}
        self.count = count


def is_crewbrain_host(host: str) -> bool:
    host = host.lower()
    return host == SERVICE_DOMAIN or host.endswith(f".{SERVICE_DOMAIN}")


def parse_stamp(value: Any) -> datetime | None:
    """A CrewBrain timestamp as an aware UTC datetime, or None."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value), STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None


def format_stamp(moment: datetime) -> str:
    """An aware datetime in CrewBrain's format. Naive input is refused."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime")
    return moment.astimezone(UTC).strftime(STAMP)


def duration_minutes(record: dict[str, Any],
                     until: datetime | None = None) -> int | None:
    """How long a record ran, from its own timestamps.

    An open record counts up to `until` when one is given.
    """
    began = parse_stamp(record.get("DateFrom"))
    ended = parse_stamp(record.get("DateUntil")) or until
    if began is None or ended is None:
        return None
    return max(0, int((ended - began).total_seconds() // 60))


def job_end(start: datetime, end: datetime | None) -> datetime:
    """When a job is over. One without a usable end lasts `NO_END`."""
    return end if end is not None and end > start else start + NO_END


def plain_text(markup: Any) -> str:
    """CrewBrain descriptions are HTML from a rich-text box. Unwrap them."""
    if not markup:
        return ""
    text = html.unescape(_TAG.sub(" ", str(markup))).replace("\xa0", " ")
    return " ".join(text.split())


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows = payload.get("data")
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
        return [payload] if payload else []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    return []


def _one(payload: Any) -> dict[str, Any]:
    """One record out of the `data` envelope, or a bare object.

    Both shapes come back, depending on the endpoint.
    """
    rows = _rows(payload)
    return rows[0] if rows else {}


def _token_in(payload: Any) -> str | None:
    """The token in `{"Accesstoken": ...}`, also inside `data`, or bare.

    The key's spelling is matched loosely: the live answer is `Accesstoken`,
    and a spelling is not a thing to fail a login on.
    """
    if isinstance(payload, str):
        return payload.strip() or None
    if isinstance(payload, list):
        return _token_in(payload[0]) if payload else None
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).replace("_", "").lower() in ("accesstoken", "token", "data"):
                found = _token_in(value)
                if found:
                    return found
    return None


def _flag(value: Any) -> bool:
    return value in (True, 1, "1", "true", "True")


def employee_id_of(me: dict[str, Any]) -> int | None:
    """The employee ID in a `/v2/me` record."""
    for key in ("EmployeeID", "ID"):
        value = me.get(key)
        if value not in (None, "", 0):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def category_title(category: dict[str, Any]) -> str:
    return str(category.get("Title") or category.get("ID")).strip()


def default_category(categories: list[dict[str, Any]]) -> int | None:
    """The category a clock-in books into unless the options name one.

    One marked for working hours, a favourite first, in CrewBrain's order.
    """
    working = [c for c in categories
               if str(c.get("Assignment") or "").upper() == "WORKINGHOURS"
               or str(c.get("Account") or "").upper() == "WORKINGHOUR"]
    pool = [c for c in working if _flag(c.get("Favorite"))] or working
    return int(pool[0]["ID"]) if pool else None


@dataclass(frozen=True)
class Job:
    """One job (CrewBrain event) the employee is on."""

    id: int
    number: str | None
    title: str
    start: datetime
    end: datetime | None
    client: str | None
    location: str | None
    city: str | None
    project: str | None
    description: str
    status: str | None
    kind: str | None
    resource_id: int | None

    @property
    def until(self) -> datetime:
        """When the job is over, also when CrewBrain gives no usable end."""
        return job_end(self.start, self.end)

    @property
    def place(self) -> str | None:
        parts = [p for p in (self.location, self.city) if p]
        return ", ".join(parts) or None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "number": self.number,
            "title": self.title,
            "start": self.start.isoformat(),
            "end": self.end.isoformat() if self.end else None,
            "client": self.client,
            "location": self.location,
            "city": self.city,
            "project": self.project,
        }


class CrewBrainApi:
    """One CrewBrain account. Every write is guarded by a read."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        token: str = "",
        *,
        employee_id: int | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._session = session
        self._origin = URL(url).origin()
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self.employee_id = employee_id
        #: ClientID -> name, and LocationID -> (name, city). A customer's
        #: name does not change between polls.
        self._clients: dict[int, str] = {}
        self._locations: dict[int, tuple[str, str]] = {}
        #: The crew of each job is one request per job. No rate-limit
        #: headers have been seen, so the ceiling is unknown; stay well
        #: under it.
        self._gate = asyncio.Semaphore(4)
        #: A clock-in or clock-out is a read, then a write. Two at once would
        #: both pass the read; one at a time, the second sees the first.
        self._punch = asyncio.Lock()

    @property
    def base_url(self) -> str:
        """Where requests go now, after any redirect."""
        return str(self._origin)

    # -- transport ---------------------------------------------------------

    async def _request(self, method: str, path: str, *,
                       params: dict[str, Any] | None = None,
                       body: dict[str, Any] | None = None,
                       auth: str | None = None) -> Any:
        query = f"?{urlencode(params)}" if params else ""
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = auth
        else:
            headers["X-API-KEY"] = self._token
        for _ in range(3):
            url = URL(f"{self._origin}/api{path}{query}", encoded=True)
            try:
                async with self._session.request(
                    method, url, json=body, headers=headers,
                    allow_redirects=False, timeout=self._timeout,
                ) as response:
                    if response.status in _REDIRECTS:
                        self._follow(response.headers.get("Location", ""))
                        continue
                    text = await response.text()
                    if response.status == 401:
                        raise CrewBrainAuthError(f"{method} {path}: refused (401)")
                    if response.status >= 400:
                        raise CrewBrainError(
                            f"{method} {path}: HTTP {response.status}")
            except (aiohttp.ClientError, TimeoutError) as err:
                raise CrewBrainError(f"{method} {path}: {err!r}") from err
            if not text.strip():
                return {}
            try:
                return json.loads(text)
            except ValueError as err:
                raise CrewBrainError(
                    f"{method} {path}: answer is not JSON") from err
        raise CrewBrainError(f"{method} {path}: too many redirects")

    def _follow(self, location: str) -> None:
        """Move to the host a redirect names, if it is CrewBrain's own."""
        target = self._origin.join(URL(location)) if location else None
        host, here = (target.host or "") if target else "", self._origin.host or ""
        same = host == here or (is_crewbrain_host(host) and is_crewbrain_host(here))
        downgrade = self._origin.scheme == "https" and target is not None \
            and target.scheme != "https"
        if target is None or not same or downgrade:
            raise CrewBrainError(f"unexpected redirect to {location or '?'}")
        _LOGGER.debug("CrewBrain moved from %s to %s", here, host)
        self._origin = target.origin()

    # -- identity ------------------------------------------------------------

    async def login(self, username: str, password: str) -> str:
        """Trade a username and password for an access token, and use it.

        The password goes into this one request and is not kept.
        """
        basic = aiohttp.BasicAuth(username, password, encoding="utf-8").encode()
        token = _token_in(await self._request("GET", "/accesstoken", auth=basic))
        if not token:
            raise CrewBrainError("GET /accesstoken: no token in the answer")
        self._token = token
        return token

    async def me(self) -> dict[str, Any]:
        return _one(await self._request("GET", "/v2/me"))

    async def time_categories(self) -> list[dict[str, Any]]:
        """The active time categories, in the order CrewBrain shows them."""
        rows = _rows(await self._request("GET", "/v2/categories/time"))
        active = [r for r in rows if r.get("ID") is not None
                  and r.get("Active", True) not in (False, 0, "0", "false")]
        return sorted(active, key=lambda r: (r.get("Position") is None,
                                             r.get("Position") or 0))

    # -- the punch clock -----------------------------------------------------

    async def open_stamps(self) -> list[dict[str, Any]]:
        """Every record that is running. Normally none or one."""
        payload = await self._request("GET", "/v2/workinghours", params={
            "EmployeeID": self.employee_id,
            "Type": "ACTUAL",
            "DateUntil[isnull]": "true",
            "sort": "-DateFrom",
            "limit": 5,
        })
        return _rows(payload)

    async def open_stamp(self) -> dict[str, Any] | None:
        """The running record, or None. Refuses to choose between two."""
        rows = await self.open_stamps()
        if len(rows) > 1:
            raise CrewBrainStateError("multiple_open", count=len(rows))
        return rows[0] if rows else None

    async def clock_in(self, when: datetime | None, activity: str,
                       category_id: int) -> dict[str, Any]:
        """Start a record, but only if none is running.

        Booked as independent time (`AssignmentID: 0`), which the live
        instance accepts. `CaptureMode` is always `API`: whether CrewBrain
        treats an API entry like its own app is for its administration to
        decide, not for this code to work around.
        """
        async with self._punch:
            running = await self.open_stamp()
            if running is not None:
                raise CrewBrainStateError("already_clocked_in", record=running)
            began = when or datetime.now(UTC)
            created = await self._request("POST", "/v2/workinghours", body={
                "EmployeeID": self.employee_id,
                "Assignment": INDEPENDENT,
                "AssignmentID": 0,
                "Type": "ACTUAL",
                "CategoryID": category_id,
                "DateFrom": format_stamp(began),
                "CaptureMode": CAPTURE_MODE,
                "Activity": activity,
            })
            # Read back: what counts is what CrewBrain stored.
            stamp = await self.open_stamp()
        _LOGGER.info("CrewBrain: clocked in, record %s",
                     (stamp or _one(created)).get("ID"))
        return stamp or _one(created)

    async def clock_out(self, when: datetime | None = None) -> dict[str, Any]:
        """Put an end time on the running record, and read it back.

        One PUT on a record that was read first. Nothing here creates a
        record.
        """
        async with self._punch:
            stamp = await self.open_stamp()
            if stamp is None:
                raise CrewBrainStateError("not_clocked_in")
            ended = when or datetime.now(UTC)
            began = parse_stamp(stamp.get("DateFrom"))
            if began is not None and ended <= began:
                raise CrewBrainStateError("end_before_start", record=stamp)
            record_id = stamp.get("ID")
            await self._request("PUT", f"/v2/workinghours/{record_id}",
                                body={"DateUntil": format_stamp(ended)})
            after = _one(await self._request("GET", f"/v2/workinghours/{record_id}"))
        _LOGGER.info("CrewBrain: clocked out, record %s", record_id)
        return after or dict(stamp, DateUntil=format_stamp(ended))

    async def entries(self, since: datetime,
                      until: datetime) -> list[dict[str, Any]]:
        """Records that start within a window, newest first."""
        payload = await self._request("GET", "/v2/workinghours", params={
            "EmployeeID": self.employee_id,
            "Type": "ACTUAL",
            "DateFrom[gte]": format_stamp(since),
            "DateFrom[lte]": format_stamp(until),
            "sort": "-DateFrom",
            "limit": 100,
        })
        return _rows(payload)

    # -- jobs ------------------------------------------------------------------

    async def my_jobs(self, since: datetime, until: datetime, *,
                      ending_after: datetime | None = None) -> list[Job]:
        """The jobs this employee is on that start within a window.

        `ending_after` drops jobs that are already over before their crew
        is read, which is what keeps a window that reaches back cheap.
        """
        payload = await self._request("GET", "/v2/events", params={
            "DateFrom[gte]": format_stamp(since),
            "DateFrom[lte]": format_stamp(until),
            "sort": "DateFrom",
            "limit": EVENTS_LIMIT,
        })
        events = [e for e in _rows(payload) if not e.get("Deleted")]
        if len(_rows(payload)) >= EVENTS_LIMIT:
            _LOGGER.warning(
                "CrewBrain returned %s jobs, its limit; later jobs in the "
                "window are missing. Shorten 'days ahead'.", EVENTS_LIMIT)
        if ending_after is not None:
            events = [e for e in events if _still_on(e, ending_after)]

        async def crewed(event: dict[str, Any]) -> dict[str, Any] | None:
            async with self._gate:
                try:
                    crew = _rows(await self._request(
                        "GET", f"/v2/events/{event['ID']}/resources"))
                except CrewBrainAuthError:
                    raise
                except CrewBrainError:
                    # One job whose crew cannot be read does not cost the
                    # whole list.
                    _LOGGER.warning("CrewBrain: crew of job %s not readable",
                                    event.get("ID"))
                    return None
            for row in crew:
                if (str(row.get("Assignment")) == "EMPLOYEE"
                        and row.get("AssignmentID") == self.employee_id):
                    return dict(event, resource_id=row.get("ID"))
            return None

        mine = [e for e in await asyncio.gather(*(crewed(e) for e in events))
                if e is not None]
        await self._name_clients(mine)
        await self._name_locations(mine)
        jobs = [job for job in (self._job(e) for e in mine) if job is not None]
        return sorted(jobs, key=lambda j: j.start)

    def _job(self, event: dict[str, Any]) -> Job | None:
        start = parse_stamp(event.get("DateFrom"))
        if start is None:
            return None
        location = self._locations.get(int(event.get("LocationID") or 0))
        parents = event.get("ParentTitles") or []
        return Job(
            id=int(event["ID"]),
            number=event.get("EventIDFormatted"),
            title=str(event.get("Title") or "").strip(),
            start=start,
            end=parse_stamp(event.get("DateUntil")),
            client=self._clients.get(int(event.get("ClientID") or 0)) or None,
            location=(location[0] if location else "") or None,
            city=(location[1] if location else "") or None,
            project=parents[0] if isinstance(parents, list) and parents else None,
            description=plain_text(event.get("Description")),
            status=event.get("Status"),
            kind=event.get("Subtype"),
            resource_id=event.get("resource_id"),
        )

    async def _name_clients(self, events: list[dict[str, Any]]) -> None:
        """A job carries `ClientID` and no name. Look each one up once."""
        wanted = {int(e["ClientID"]) for e in events
                  if e.get("ClientID") and int(e["ClientID"]) not in self._clients}

        async def name(client_id: int) -> None:
            async with self._gate:
                try:
                    record = _one(await self._request(
                        "GET", f"/v2/clients/{client_id}"))
                except CrewBrainAuthError:
                    raise
                except CrewBrainError:
                    _LOGGER.warning("CrewBrain: client %s not readable", client_id)
                    return
            self._clients[client_id] = str(record.get("Name") or "").strip()

        await asyncio.gather(*(name(c) for c in wanted))

    async def _name_locations(self, events: list[dict[str, Any]]) -> None:
        """Where the work happens. Often not the client, and both are kept."""
        wanted = {int(e["LocationID"]) for e in events
                  if e.get("LocationID")
                  and int(e["LocationID"]) not in self._locations}

        async def one(location_id: int) -> None:
            async with self._gate:
                try:
                    record = _one(await self._request(
                        "GET", f"/v2/locations/{location_id}"))
                except CrewBrainAuthError:
                    raise
                except CrewBrainError:
                    _LOGGER.warning("CrewBrain: location %s not readable",
                                    location_id)
                    return
            self._locations[location_id] = (
                str(record.get("Description") or "").strip(),
                str(record.get("City") or "").strip(),
            )

        await asyncio.gather(*(one(i) for i in wanted))


def _still_on(event: dict[str, Any], after: datetime) -> bool:
    """Whether a job is not over by `after`, by the same rule as `Job.until`."""
    start = parse_stamp(event.get("DateFrom"))
    return start is not None and job_end(start, parse_stamp(event.get("DateUntil"))) >= after
