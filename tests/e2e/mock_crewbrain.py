"""A stand-in CrewBrain for the end-to-end test. Never the real one.

Answers under three host names (network aliases in docker-compose.yml):

- example.crewbrain.com      every path -> 308 to the region host, as live
- example.eu2.crewbrain.com  the API, records held in memory
- evil.example               logs anything that reaches it

Paths starting with /__ are controls for the test runner.
"""

import base64
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

FIRM = "example.crewbrain.com"
REGION = "example.eu2.crewbrain.com"
EVIL = "evil.example"
EMPLOYEE = 42
USERNAME = "t.person"
#: A colon, a space and an umlaut: Basic auth has to carry all three.
PASSWORD = "Pä:ss wörd"

STATE = {
    "key": "test:key",
    "evil": False,
    "delay": 0.0,
    "next_id": 1000,
    "records": [],
    "events": [],
    "crew": {},
    "clients": {},
    "locations": {},
    "categories": [],
    "roles": {},
    "account": {},
    "absences": [],
    "absence_kinds": [],
    "forbidden": [],
    "log": [],
    "evil_log": [],
}


def _basic(header):
    """(user, password) from a Basic auth header, or None."""
    if not header.startswith("Basic "):
        return None
    try:
        user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except ValueError:
        return None
    return user, password


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body=None, content_type="application/json", headers=None):
        raw = b"" if body is None else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self, method):
        url = urlsplit(self.path)
        host = (self.headers.get("Host") or "").split(":")[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        if url.path.startswith("/__"):
            return self._control(url.path, body)
        entry = {"method": method, "host": host, "path": url.path,
                 "query": url.query, "body": body,
                 "key_ok": self.headers.get("X-API-KEY") == STATE["key"],
                 "has_key": bool(self.headers.get("X-API-KEY")),
                 "basic": _basic(self.headers.get("Authorization") or "") is not None}
        if host == EVIL:
            STATE["evil_log"].append(entry)
            return self._send(200, {"data": []})
        STATE["log"].append(entry)
        if host == FIRM:
            return self._send(308, None, headers={
                "Location": f"http://{REGION}:8080{self.path}"})
        if url.path == "/api/accesstoken" and method == "GET":
            # The live instance labels this JSON text/html, too.
            if _basic(self.headers.get("Authorization") or "") != (USERNAME, PASSWORD):
                return self._send(401, {"error": "invalid credentials"})
            return self._send(200, {"Accesstoken": STATE["key"]}, content_type="text/html")
        if self.headers.get("X-API-KEY") != STATE["key"]:
            return self._send(401, {"error": "invalid api key"})
        if any(url.path.startswith(prefix) for prefix in STATE["forbidden"]):
            return self._send(403, {"error": "no access"})
        if (STATE["evil"] and method == "GET"
                and url.path.startswith("/api/v2/workinghours")):
            return self._send(308, None, headers={
                "Location": f"http://{EVIL}:8080{self.path}"})
        return self._api(method, url.path, parse_qs(url.query), body)

    def _api(self, method, path, q, body):
        if path == "/api/v2/me" and method == "GET":
            # The live instance labels some JSON answers text/html.
            return self._send(200, {"data": [{
                "ID": EMPLOYEE, "Firstname": "Test", "Lastname": "Person",
                "Username": USERNAME}]}, content_type="text/html")
        if path == "/api/v2/categories/time" and method == "GET":
            return self._send(200, {"data": STATE["categories"]})
        m = re.fullmatch(r"/api/v2/categories/generic/(\d+)", path)
        if m and method == "GET" and m.group(1) in STATE["roles"]:
            return self._send(200, {"data": [{"ID": int(m.group(1)), "Title": STATE["roles"][m.group(1)]}]})
        if path == "/api/v2/categories/vacation" and method == "GET":
            return self._send(200, {"data": STATE["absence_kinds"]})
        if path == "/api/v2/absenceaccounts" and method == "GET":
            # A bare object, as the spec has it.
            return self._send(200, STATE["account"])
        if path == "/api/v2/absences" and method == "GET":
            rows = STATE["absences"]
            for field in ("Assignment", "AssignmentID"):
                if field in q:
                    rows = [r for r in rows if str(r.get(field)) == q[field][0]]
            if "Until[gte]" in q:
                rows = [r for r in rows if r["Until"] >= q["Until[gte]"][0]]
            if "From[lte]" in q:
                rows = [r for r in rows if r["From"] <= q["From[lte]"][0]]
            rows = sorted(rows, key=lambda r: r["From"])
            return self._send(200, {"data": rows[: int(q.get("limit", ["100"])[0])]})
        m = re.fullmatch(r"/api/v2/workinghours(?:/(\d+))?", path)
        if m:
            return self._workinghours(method, int(m.group(1)) if m.group(1) else None, q, body)
        if path == "/api/v2/events" and method == "GET":
            if "DateFrom[gte]" not in q or "DateFrom[lte]" not in q:
                return self._send(400, {"error": "Value for field RetailPriceType is invalid."})
            lo, hi = q["DateFrom[gte]"][0], q["DateFrom[lte]"][0]
            rows = [e for e in STATE["events"] if lo <= e["DateFrom"] <= hi]
            rows.sort(key=lambda e: e["DateFrom"])
            return self._send(200, {"data": rows[: int(q.get("limit", ["100"])[0])]})
        m = re.fullmatch(r"/api/v2/events/(\d+)/resources", path)
        if m and method == "GET":
            return self._send(200, {"data": STATE["crew"].get(m.group(1), [])})
        m = re.fullmatch(r"/api/v2/clients/(\d+)", path)
        if m and method == "GET" and m.group(1) in STATE["clients"]:
            return self._send(200, {"data": [{"Name": STATE["clients"][m.group(1)]}]})
        m = re.fullmatch(r"/api/v2/locations/(\d+)", path)
        if m and method == "GET" and m.group(1) in STATE["locations"]:
            name, city = STATE["locations"][m.group(1)]
            return self._send(200, {"data": [{"Description": name, "City": city}]})
        return self._send(404, {"error": "no route"})

    def _workinghours(self, method, rid, q, body):
        records = STATE["records"]
        if method == "GET" and rid is None:
            if q.get("DateUntil[isnull]") == ["true"]:
                # A slow answer lets two clock-ins overlap, if nothing
                # keeps them apart.
                time.sleep(STATE["delay"])
            rows = [r for r in records if str(r["EmployeeID"]) == q.get("EmployeeID", [""])[0]]
            if q.get("DateUntil[isnull]") == ["true"]:
                rows = [r for r in rows if r["DateUntil"] is None]
            if "DateFrom[gte]" in q:
                rows = [r for r in rows if r["DateFrom"] >= q["DateFrom[gte]"][0]]
            if "DateFrom[lte]" in q:
                rows = [r for r in rows if r["DateFrom"] <= q["DateFrom[lte]"][0]]
            rows.sort(key=lambda r: r["DateFrom"], reverse=True)
            return self._send(200, {"data": rows[: int(q.get("limit", ["100"])[0])]})
        if method == "GET":
            rows = [r for r in records if r["ID"] == rid]
            return self._send(200 if rows else 404, {"data": rows})
        if method == "POST" and rid is None:
            record = json.loads(body)
            STATE["next_id"] += 1
            record.update(ID=STATE["next_id"], DateUntil=None, Duration=None)
            records.append(record)
            return self._send(200, {"data": [record]})
        if method == "PUT" and rid is not None:
            for record in records:
                if record["ID"] == rid:
                    record.update(json.loads(body))
                    return self._send(200, None)
            return self._send(404, {"error": "no record"})
        return self._send(405, {"error": "method"})

    def _control(self, path, body):
        data = json.loads(body) if body else {}
        if path == "/__seed":
            STATE.update({k: data[k] for k in
                          ("records", "events", "crew", "clients", "locations", "categories",
                           "roles", "account", "absences", "absence_kinds")
                          if k in data})
        elif path == "/__two_open":
            for _ in range(2):
                STATE["next_id"] += 1
                STATE["records"].append({"ID": STATE["next_id"], "EmployeeID": EMPLOYEE,
                                         "DateFrom": "20261019T050000Z", "DateUntil": None})
        elif path == "/__clear_open":
            STATE["records"] = [r for r in STATE["records"] if r["DateUntil"] is not None]
        elif path == "/__evil":
            STATE["evil"] = bool(data.get("on"))
        elif path == "/__forbid":
            STATE["forbidden"] = list(data.get("paths", []))
        elif path == "/__delay":
            STATE["delay"] = float(data.get("seconds", 0))
        elif path == "/__set_key":
            STATE["key"] = data["key"]
        elif path == "/__dump":
            return self._send(200, {k: STATE[k] for k in ("records", "log", "evil_log")})
        else:
            return self._send(404, {})
        return self._send(200, {})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
