"""End-to-end test: the integration in a real Home Assistant, against the
stand-in CrewBrain in mock_crewbrain.py. Standard library only.

    docker compose -f tests/e2e/docker-compose.yml up --abort-on-container-exit --exit-code-from runner
    docker compose -f tests/e2e/docker-compose.yml down -v
"""

import base64
import json
import math
import os
import re
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HA = "http://homeassistant:8123"
FIRM = "http://example.crewbrain.com:8080"
MOCK = "http://example.eu2.crewbrain.com:8080"
#: Home Assistant's time zone in configuration.yaml.
LOCAL = ZoneInfo("Europe/Berlin")
USERNAME = "t.person"
PASSWORD = "Pä:ss wörd"
STAMP = "%Y%m%dT%H%M%SZ"
TOKEN = None
WS = None
FAILED = []


def call(method, url, body=None, form=None, auth=True):
    data, headers = None, {}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if auth and TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw, status = response.read().decode(), response.status
    except urllib.error.HTTPError as err:
        raw, status = err.read().decode(), err.code
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name + ("" if condition else f"  -- {detail}"))
    if not condition:
        FAILED.append(name)


def mock(path, body=None):
    return call("POST", f"{MOCK}/__{path}", body or {}, auth=False)[1]


def requests_since(mark):
    return mock("dump")["log"][mark:]


def log_mark():
    return len(mock("dump")["log"])


def states():
    return {s["entity_id"]: s for s in call("GET", f"{HA}/api/states")[1]}


def state(entity):
    return states().get(entity, {})


def service(domain, name, data, response=False):
    """Call an action the way the UI and an AI do: over the WebSocket API.

    Not over REST, whose answer to a refused action differs between Home
    Assistant versions (500 on 2025.8, 400 on 2026.10). Returned in REST's
    shape: (200, {"service_response": ...}) or (400, {"message": ...}).
    """
    request = {"domain": domain, "service": name, "service_data": data}
    if response:
        request["return_response"] = True
    reply = WS.command_full("call_service", **request)
    if reply.get("success"):
        return 200, {"service_response": (reply.get("result") or {}).get("response")}
    return 400, {"message": (reply.get("error") or {}).get("message", "")}


def refresh(entity, what, condition):
    """Ask for a refresh and wait for its effect.

    Home Assistant delays a manual refresh by up to ten seconds after one
    that ran just before, so the effect is waited for, not slept on.
    """
    service("homeassistant", "update_entity", {"entity_id": entity})
    return wait(what, condition, 30)


class WebSocket:
    """Just enough of a WebSocket client for Home Assistant's API."""

    def __init__(self, host, port, token):
        self.sock = socket.create_connection((host, port), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET /api/websocket HTTP/1.1\r\nHost: {host}:{port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.sock.recv(1)
        self.buf, self.id = b"", 0
        self.recv()
        self.send({"type": "auth", "access_token": token})
        assert self.recv()["type"] == "auth_ok"

    def send(self, obj):
        data, mask = json.dumps(obj).encode(), os.urandom(4)
        n = len(data)
        head = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else
                                bytes([0x80 | 126]) + struct.pack(">H", n))
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        payload = b""
        while True:
            first, second = self._read(2)
            n = second & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload += self._read(n)
            if first & 0x80:
                return json.loads(payload)

    def command_full(self, kind, **data):
        self.id += 1
        self.send({"id": self.id, "type": kind, **data})
        while True:
            message = self.recv()
            if message.get("id") == self.id and message.get("type") == "result":
                return message

    def command(self, kind, **data):
        return self.command_full(kind, **data).get("result")

    def command_many(self, commands):
        """Send several commands before reading any answer, so they run at once."""
        ids = []
        for kind, data in commands:
            self.id += 1
            ids.append(self.id)
            self.send({"id": self.id, "type": kind, **data})
        results = {}
        while len(results) < len(ids):
            message = self.recv()
            if message.get("type") == "result" and message.get("id") in ids:
                results[message["id"]] = message
        return [results[i] for i in ids]


def stamp(moment):
    return moment.astimezone(UTC).strftime(STAMP)


def wait(what, condition, seconds=90):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if condition():
                return True
        except Exception:
            pass
        time.sleep(1)
    check(f"waited for {what}", False, "timed out")
    return False


def message(response):
    body = response[1]
    return body.get("message", "") if isinstance(body, dict) else str(body)


def stored_entry():
    """Our config entry as Home Assistant wrote it to disk, and the raw file."""
    raw = Path("/storage/core.config_entries").read_text(encoding="utf-8")
    entries = json.loads(raw)["data"]["entries"]
    return next((x for x in entries if x["domain"] == "crewbrain"), {}), raw


def seconds_within(start, end, lo, hi):
    return max(0.0, (min(end, hi) - max(start, lo)).total_seconds())


def shown_minutes(entity):
    """Minutes as the frontend shows a duration in hours: h and floor(min)."""
    hours = float(entity["state"])
    whole = math.floor(hours)
    return whole * 60 + math.floor((hours - whole) * 60)


def main():
    global TOKEN, WS
    wait("Home Assistant", lambda: call("GET", f"{HA}/api/onboarding", auth=False)[0] == 200, 300)
    _, user = call("POST", f"{HA}/api/onboarding/users", {
        "client_id": f"{HA}/", "name": "Test", "username": "test",
        "password": "test-password-1", "language": "en"}, auth=False)
    _, token = call("POST", f"{HA}/auth/token", form={
        "grant_type": "authorization_code", "code": user["auth_code"],
        "client_id": f"{HA}/"}, auth=False)
    TOKEN = token["access_token"]
    WS = WebSocket("homeassistant", 8123, TOKEN)

    # -- seed -------------------------------------------------------------------
    now = datetime.now(UTC).replace(microsecond=0)
    minute = timedelta(minutes=1)
    hour = timedelta(hours=1)
    day = timedelta(days=1)
    midnight = datetime.combine(now.astimezone(LOCAL).date(), datetime.min.time(), LOCAL)
    events = {
        "1": ("A mine tomorrow", now + day, now + day + 8 * hour, False),
        "2": ("B someone else's", now + 2 * day, now + 2 * day + hour, False),
        "3": ("C deleted", now + 3 * day, now + 3 * day + hour, True),
        "4": ("D mine running", now - 2 * day, now + day, False),
        "5": ("E mine in 20 days", now + 20 * day, now + 20 * day + hour, False),
        "6": ("F mine over", now - 3 * day, now - 2 * day, False),
        "7": ("G mine without an end", now - 10 * minute, None, False),
    }
    # One record today, and one from last night that ran past midnight.
    records = [(900, now - 30 * minute, now - 10 * minute),
               (901, midnight - 60 * minute, midnight + 30 * minute)]
    worked = int(sum(seconds_within(a, b, midnight, midnight + day) for _, a, b in records) // 60)
    mock("seed", {
        "records": [{"ID": i, "EmployeeID": 42, "DateFrom": stamp(a), "DateUntil": stamp(b)}
                    for i, a, b in records],
        "events": [{"ID": int(i), "EventIDFormatted": f"J-{i}", "Title": t,
                    "DateFrom": stamp(a), "DateUntil": stamp(b) if b else None, "Deleted": d,
                    "ClientID": 300, "LocationID": 7 if i == "1" else None,
                    "ParentTitles": ["Project X"],
                    "Description": "<div>Raum&nbsp;C 201</div><div>Bühne</div>"}
                   for i, (t, a, b, d) in events.items()],
        "crew": {i: [{"ID": 5100 + int(i), "Assignment": "EMPLOYEE",
                      "AssignmentID": 99 if i == "2" else 42}] for i in events},
        "clients": {"300": "Example Client"},
        "locations": {"7": ["Hall 3", "Example City"]},
        # Each company has its own IDs; the default is found by kind and order.
        "categories": [
            {"ID": 10, "Title": "Break", "Assignment": "BREAKS", "Position": 0},
            {"ID": 11, "Title": "Old hours", "Assignment": "WORKINGHOURS", "Active": False, "Position": 0},
            {"ID": 13, "Title": "Driving time", "Assignment": "WORKINGHOURS", "Position": 2},
            {"ID": 12, "Title": "Working hours", "Assignment": "WORKINGHOURS", "Position": 1},
        ],
    })

    # -- config flow --------------------------------------------------------------
    flows = f"{HA}/api/config/config_entries/flow"

    def new_flow():
        return call("POST", flows, {"handler": "crewbrain"})[1]["flow_id"]

    def login(flow_id, url, username=USERNAME, password=PASSWORD):
        return call("POST", f"{flows}/{flow_id}",
                    {"url": url, "username": username, "password": password})[1]

    fid = new_flow()
    for bad in ("ftp://", "http://evil.example:8080", "not a name"):
        step = login(fid, bad)
        check(f"config: address {bad!r} refused", step.get("errors", {}).get("url") == "invalid_url", step)
    step = login(fid, FIRM, password="wrong")
    check("config: wrong password refused", step.get("errors", {}).get("base") == "invalid_auth", step)
    mark = log_mark()
    step = login(fid, FIRM, username=f" {USERNAME} ")
    check("config: entry created", step.get("type") == "create_entry", step)
    check("config: title is the employee", step.get("title") == "CrewBrain Test Person", step.get("title"))
    entry_id = step["result"]["entry_id"]
    setup_seen = requests_since(mark)
    again = login(new_flow(), FIRM)
    check("config: same account twice refused", again.get("reason") == "already_configured", again)

    wait("entry on disk", lambda: stored_entry()[0].get("data", {}).get("api_token") == "test:key", 30)
    stored, raw = stored_entry()
    data = stored.get("data", {})
    check("config: the token is stored, the password is not",
          "password" not in data and PASSWORD not in raw and json.dumps(PASSWORD)[1:-1] not in raw,
          sorted(data))
    check("config: username kept for signing in again", data.get("username") == USERNAME, data)

    p = "crewbrain_test_person"
    e = {k: f"{d}.{p}_{name}" for d, k, name in [
        ("binary_sensor", "clocked_in", "status"), ("sensor", "clocked_in_since", "clocked_in_since"),
        ("sensor", "worked_today", "worked_today"), ("sensor", "next_job", "next_job"),
        ("switch", "punch_clock", "punch_clock"), ("calendar", "jobs", "jobs")]}
    print("entities:", sorted(e.values()))

    polling_mark = log_mark()
    wait("entities", lambda: all(state(x).get("state") not in (None, "unavailable") for x in e.values()))
    seen = setup_seen + requests_since(polling_mark)
    check("redirect: first request went to the firm host",
          setup_seen and setup_seen[0]["host"] == "example.crewbrain.com", setup_seen[:1])
    check("redirect: everything after it goes to the region host",
          all(r["host"] == "example.eu2.crewbrain.com" for r in seen[1:]),
          [(r["host"], r["path"]) for r in seen[1:] if r["host"] != "example.eu2.crewbrain.com"])
    check("login: the password went into the token request only",
          any(r["basic"] for r in seen)
          and all(r["path"] == "/api/accesstoken" for r in seen if r["basic"]),
          [r["path"] for r in seen if r["basic"]])
    check("every other request carried the key",
          all(r["key_ok"] for r in seen if r["path"] != "/api/accesstoken"))
    crew_reads = [r["path"] for r in seen if r["path"].endswith("/resources")]
    check("jobs: crew not read for a job already over", "/api/v2/events/6/resources" not in crew_reads, crew_reads)
    check("jobs: crew not read for a deleted job", "/api/v2/events/3/resources" not in crew_reads, crew_reads)
    event_query = next((r["query"] for r in seen if r["path"] == "/api/v2/events"), "")
    check("jobs: events asked with a date range", "DateFrom%5Bgte%5D" in event_query, event_query)
    open_query = next((r["query"] for r in seen if r["path"] == "/api/v2/workinghours"
                       and "isnull" in r["query"]), "")
    check("clock: open record asked as live", "DateUntil%5Bisnull%5D=true" in open_query
          and "EmployeeID=42" in open_query and "Type=ACTUAL" in open_query, open_query)

    # -- initial state ------------------------------------------------------------
    s = states()
    check("not clocked in", s[e["clocked_in"]]["state"] == "off", s[e["clocked_in"]])
    check("switch off", s[e["punch_clock"]]["state"] == "off", s[e["punch_clock"]])
    check("since: not clocked in, not unknown", s[e["clocked_in_since"]]["state"] == "not_clocked_in"
          and s[e["clocked_in_since"]]["attributes"].get("since") is None, s[e["clocked_in_since"]])
    check(f"worked today {worked} min, last night's part after midnight included",
          shown_minutes(s[e["worked_today"]]) == worked, s[e["worked_today"]])
    check("worked today in hours", s[e["worked_today"]]["attributes"].get("unit_of_measurement") == "h",
          s[e["worked_today"]]["attributes"])
    nxt = s[e["next_job"]]
    titles = [j["title"] for j in nxt["attributes"].get("upcoming", [])]
    check("next job is the running one", nxt["attributes"].get("title") == "D mine running", nxt["attributes"].get("title"))
    check("upcoming: mine only, not over, not deleted, within 14 days, one without an end lasts an hour",
          titles == ["D mine running", "G mine without an end", "A mine tomorrow"], titles)
    check("next job: client named", nxt["attributes"].get("client") == "Example Client", nxt["attributes"])
    check("next job: description without markup", nxt["attributes"].get("description") == "Raum C 201 Bühne",
          nxt["attributes"].get("description"))

    # -- calendar -------------------------------------------------------------------
    def calendar(a, b):
        q = urllib.parse.urlencode({"start": a.isoformat(), "end": b.isoformat()})
        return call("GET", f"{HA}/api/calendars/{e['jobs']}?{q}")[1]
    cal = calendar(now - day, now + 10 * day)
    check("calendar: this window",
          [c["summary"] for c in cal] == ["D mine running", "G mine without an end", "A mine tomorrow"],
          [c.get("summary") for c in cal])
    a = next((c for c in cal if c["summary"] == "A mine tomorrow"), {})
    check("calendar: location is site and town", a.get("location") == "Hall 3, Example City", a.get("location"))
    check("calendar: description in Home Assistant's language",
          "Client: Example Client" in (a.get("description") or "")
          and "<" not in (a.get("description") or ""), a.get("description"))
    far = calendar(now + 15 * day, now + 25 * day)
    check("calendar: outside the polled window is read on demand",
          [c["summary"] for c in far] == ["E mine in 20 days"], far)
    check("calendar state is the running job", state(e["jobs"]).get("attributes", {}).get("message") == "D mine running",
          state(e["jobs"]).get("attributes"))

    # -- switch ---------------------------------------------------------------------
    mark = log_mark()
    r = service("switch", "turn_on", {"entity_id": e["punch_clock"]})
    check("switch on: accepted", r[0] == 200, r)
    posts = [x for x in requests_since(mark) if x["method"] == "POST"]
    check("switch on: one POST", len(posts) == 1, posts)
    body = json.loads(posts[0]["body"]) if posts else {}
    check("switch on: payload as live", {k: body.get(k) for k in
          ("EmployeeID", "Assignment", "AssignmentID", "Type", "CategoryID", "CaptureMode", "Activity")} == {
          "EmployeeID": 42, "Assignment": "INDEPENDENTHOURS", "AssignmentID": 0, "Type": "ACTUAL",
          "CategoryID": 12, "CaptureMode": "API", "Activity": "Home Assistant"}, body)
    check("switch on: DateFrom is UTC now", bool(re.fullmatch(r"\d{8}T\d{6}Z", body.get("DateFrom", "")))
          and abs((datetime.strptime(body["DateFrom"], STAMP).replace(tzinfo=UTC) - datetime.now(UTC)).total_seconds()) < 60,
          body.get("DateFrom"))
    check("switch on: read back before it returned",
          [x["method"] for x in requests_since(mark)][:3] == ["GET", "POST", "GET"], [x["method"] for x in requests_since(mark)])
    s = states()
    check("clocked in now", s[e["clocked_in"]]["state"] == "on" and s[e["punch_clock"]]["state"] == "on",
          (s[e["clocked_in"]]["state"], s[e["punch_clock"]]["state"]))
    since = s[e["clocked_in_since"]]
    check("since: the local time, the moment in the attribute",
          re.fullmatch(r"\d\d:\d\d|\d{4}-\d\d-\d\d \d\d:\d\d", since["state"])
          and datetime.fromisoformat(since["attributes"].get("since") or "x").astimezone(LOCAL).strftime("%H:%M")
          == since["state"][-5:], since)

    mark = log_mark()
    r = service("switch", "turn_on", {"entity_id": e["punch_clock"]})
    check("switch on twice: refused", r[0] == 400 and "already clocked in" in message(r), r)
    check("switch on twice: nothing written", not [x for x in requests_since(mark) if x["method"] != "GET"])

    mark = log_mark()
    r = service("crewbrain", "clock_out", {"time": "2000-01-01 08:00:00"})
    check("clock out before the start: refused", r[0] == 400 and "before the start" in message(r), r)
    check("clock out before the start: nothing written", not [x for x in requests_since(mark) if x["method"] != "GET"])

    mark = log_mark()
    r = service("switch", "turn_off", {"entity_id": e["punch_clock"]})
    check("switch off: accepted", r[0] == 200, r)
    writes = [x for x in requests_since(mark) if x["method"] != "GET"]
    check("switch off: exactly one PUT, no POST", [x["method"] for x in writes] == ["PUT"], writes)
    check("switch off: PUT on the open record", writes and re.fullmatch(r"/api/v2/workinghours/\d+", writes[0]["path"]), writes)
    s = states()
    check("clocked out", s[e["clocked_in"]]["state"] == "off" and s[e["punch_clock"]]["state"] == "off")
    check("worked today still counted", shown_minutes(s[e["worked_today"]]) >= worked, s[e["worked_today"]])

    r = service("switch", "turn_off", {"entity_id": e["punch_clock"]})
    check("switch off twice: refused", r[0] == 400 and "not clocked in" in message(r), r)

    # -- two clock-ins at once ----------------------------------------------------------
    # The read before each write is slowed down, so without a lock both
    # reads would see no open record and both would write one.
    mock("delay", {"seconds": 0.5})
    mark = log_mark()
    replies = WS.command_many([
        ("call_service", {"domain": "switch", "service": "turn_on",
                          "service_data": {"entity_id": e["punch_clock"]}}),
        ("call_service", {"domain": "crewbrain", "service": "clock_in", "service_data": {}}),
    ])
    mock("delay", {"seconds": 0})
    posts = [x for x in requests_since(mark) if x["method"] == "POST"]
    check("at once: one record, not two", len(posts) == 1, len(posts))
    check("at once: the second refused as already clocked in",
          [bool(x.get("success")) for x in replies].count(True) == 1
          and any("already clocked in" in (x.get("error") or {}).get("message", "") for x in replies),
          replies)
    service("crewbrain", "clock_out", {})

    # -- actions with times -----------------------------------------------------------
    def punch(name, data, until_mark):
        r = service("crewbrain", name, data, response=True)
        sent = [x for x in requests_since(until_mark) if x["method"] in ("POST", "PUT")]
        return r, json.loads(sent[-1]["body"]) if sent else {}

    for label, start, end, utc_from, utc_until, minutes in [
        ("summer time", "2026-10-01 08:00:00", "2026-10-01 16:30:00", "20261001T060000Z", "20261001T143000Z", 510),
        ("winter time, 7 minutes", "2026-12-01 08:00:00", "2026-12-01 08:07:00", "20261201T070000Z", "20261201T070700Z", 7),
    ]:
        mark = log_mark()
        r, sent = punch("clock_in", {"time": start, "note": 'Bühne "Saal 2"'}, mark)
        check(f"{label}: clock in accepted", r[0] == 200, r)
        check(f"{label}: start sent as UTC", sent.get("DateFrom") == utc_from, sent)
        check(f"{label}: note is the activity", sent.get("Activity") == 'Bühne "Saal 2"', sent)
        response = (r[1] or {}).get("service_response", {}) if isinstance(r[1], dict) else {}
        check(f"{label}: response has the record", response.get("record_id") and response.get("end") is None, response)
        mark = log_mark()
        r, sent = punch("clock_out", {"time": end}, mark)
        response = (r[1] or {}).get("service_response", {}) if isinstance(r[1], dict) else {}
        check(f"{label}: clock out accepted", r[0] == 200, r)
        check(f"{label}: end sent as UTC", sent.get("DateUntil") == utc_until, sent)
        check(f"{label}: minutes from the timestamps", response.get("minutes") == minutes, response)

    # -- two open records ---------------------------------------------------------------
    mock("two_open")
    refresh(e["clocked_in"], "two open records shown",
            lambda: state(e["clocked_in"])["attributes"].get("open_records") == 2)
    s = states()
    check("two open: shown as clocked in", s[e["clocked_in"]]["state"] == "on", s[e["clocked_in"]])
    check("two open: count in attributes", s[e["clocked_in"]]["attributes"].get("open_records") == 2, s[e["clocked_in"]])
    check("two open: since unknown", s[e["clocked_in_since"]]["state"] == "unknown", s[e["clocked_in_since"]])
    mark = log_mark()
    r = service("switch", "turn_off", {"entity_id": e["punch_clock"]})
    check("two open: clock out refused", r[0] == 400 and "2 records are open" in message(r), r)
    r = service("crewbrain", "clock_in", {})
    check("two open: clock in refused", r[0] == 400 and "2 records are open" in message(r), r)
    check("two open: nothing written", not [x for x in requests_since(mark) if x["method"] != "GET"])
    mock("clear_open")
    refresh(e["clocked_in"], "open records cleared", lambda: state(e["clocked_in"])["state"] == "off")

    # -- options ----------------------------------------------------------------------
    opts = f"{HA}/api/config/config_entries/options/flow"
    _, flow = call("POST", opts, {"handler": entry_id})
    field = next((f for f in flow.get("data_schema", []) if f.get("name") == "category"), {})
    offered = [o.get("label") for o in field.get("selector", {}).get("select", {}).get("options", [])]
    check("options: categories offered in CrewBrain's order, inactive left out",
          offered == ["Break", "Working hours", "Driving time"], offered)
    check("options: default category is the first working-hours one", field.get("default") == "12", field)
    _, done = call("POST", f"{opts}/{flow['flow_id']}",
                   {"days_ahead": 30, "activity": "Büro", "category": "13"})
    check("options: saved", done.get("type") == "create_entry", done)
    wait("reload with 30 days", lambda: "E mine in 20 days" in
         [j["title"] for j in state(e["next_job"])["attributes"].get("upcoming", [])])
    mark = log_mark()
    service("switch", "turn_on", {"entity_id": e["punch_clock"]})
    posts = [json.loads(x["body"]) for x in requests_since(mark) if x["method"] == "POST"]
    check("options: activity text used", posts and posts[0].get("Activity") == "Büro", posts)
    check("options: category used", posts and posts[0].get("CategoryID") == 13, posts)
    service("switch", "turn_off", {"entity_id": e["punch_clock"]})

    # -- a redirect elsewhere -------------------------------------------------------------
    mock("evil", {"on": True})
    refresh(e["clocked_in"], "foreign redirect refused",
            lambda: state(e["clocked_in"]).get("state") == "unavailable")
    check("foreign redirect: entities unavailable", state(e["clocked_in"]).get("state") == "unavailable",
          state(e["clocked_in"]).get("state"))
    evil = mock("dump")["evil_log"]
    check("foreign redirect: nothing reached the foreign host", evil == [], evil)
    mock("evil", {"on": False})
    check("foreign redirect: back afterwards",
          refresh(e["clocked_in"], "back after the redirect", lambda: state(e["clocked_in"]).get("state") == "off"))

    # -- a revoked token --------------------------------------------------------------------
    mock("set_key", {"key": "new:key"})
    service("homeassistant", "update_entity", {"entity_id": e["clocked_in"]})
    def reauth():
        progress = WS.command("config_entries/flow/progress") or []
        return next((f for f in progress if f.get("handler") == "crewbrain"
                     and f.get("context", {}).get("source") == "reauth"), None)
    check("revoked token: reauth flow started", wait("reauth flow", lambda: reauth() is not None, 30))
    flow = reauth()
    if flow:
        _, form = call("GET", f"{flows}/{flow['flow_id']}")
        user_field = next((f for f in form.get("data_schema", []) if f.get("name") == "username"), {})
        check("reauth: username filled in", user_field.get("default") == USERNAME, form)
        _, step = call("POST", f"{flows}/{flow['flow_id']}",
                       {"username": USERNAME, "password": "still wrong"})
        check("reauth: wrong password refused", step.get("errors", {}).get("base") == "invalid_auth", step)
        before = len(call("GET", f"{HA}/api/error_log")[1])
        _, step = call("POST", f"{flows}/{flow['flow_id']}",
                       {"username": USERNAME, "password": PASSWORD})
        check("reauth: signed in again", step.get("reason") == "reauth_successful", step)
        wait("entities back", lambda: state(e["clocked_in"]).get("state") == "off")
        time.sleep(3)
        since = call("GET", f"{HA}/api/error_log")[1][before:]
        setups = since.count("Finished fetching crewbrain jobs data")
        check("reauth: the entry is set up once, not twice", setups == 1, setups)
        check("reauth: the new token stored",
              wait("new token on disk",
                   lambda: stored_entry()[0].get("data", {}).get("api_token") == "new:key", 30))

    # -- translations and the log -----------------------------------------------------------
    def keys(node, prefix=""):
        if isinstance(node, dict):
            return {k2 for k, v in node.items() for k2 in keys(v, f"{prefix}{k}.")}
        return {prefix.rstrip(".")}
    en = json.loads(Path("/component/translations/en.json").read_text(encoding="utf-8"))
    de = json.loads(Path("/component/translations/de.json").read_text(encoding="utf-8"))
    check("translations: de has the same keys as en", keys(en) == keys(de), keys(en) ^ keys(de))
    served = (WS.command("frontend/get_translations", language="de", category="entity",
                         integration=["crewbrain"]) or {}).get("resources", {})
    wanted = {
        "binary_sensor.clocked_in.name": "Status",
        "binary_sensor.clocked_in.state.off": "Ausgestempelt",
        "sensor.clocked_in_since.state.not_clocked_in": "Nicht eingestempelt",
        "switch.punch_clock.state.on": "Eingestempelt",
        "calendar.jobs.state.off": "Kein Job",
    }
    check("translations: the states' German wording is served to the frontend",
          all(served.get(f"component.crewbrain.entity.{k}") == v for k, v in wanted.items()),
          {k: served.get(f"component.crewbrain.entity.{k}") for k in wanted})

    _, log = call("GET", f"{HA}/api/error_log")
    log = log if isinstance(log, str) else ""
    ours = [line for line in log.splitlines() if "crewbrain" in line.lower()]
    print("\n-- log lines mentioning crewbrain --")
    print("\n".join(ours[-40:]))
    # A traceback is expected only as the debug detail ("Full error") of the
    # two failures provoked above. Anything else is a bug.
    records = re.split(r"\n(?=\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", log)
    stray = [rec.splitlines()[0] for rec in records
             if "Traceback" in rec and not ("DEBUG" in rec.splitlines()[0] and "Full error" in rec)]
    check("log: no traceback except the provoked failures' debug detail", not stray, stray[:3])
    check("log: no blocking call", "Detected blocking call" not in log)
    check("log: token never logged", "test:key" not in log and "new:key" not in log)
    check("log: password never logged", PASSWORD not in log and "still wrong" not in log)

    print(f"\n{len(FAILED)} failed" if FAILED else "\nall passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
