# CrewBrain for Home Assistant

A custom integration for [CrewBrain](https://www.crewbrain.com): your punch
clock and your jobs. Not affiliated with CrewBrain.

## Install

**HACS:** add this repository as a custom repository (category
Integration), install **CrewBrain**, restart Home Assistant.

**By hand:** copy `custom_components/crewbrain` into your Home Assistant
`config/custom_components/`, restart.

## Set up

*Settings → Devices & services → Add integration → CrewBrain*, then enter:

- **CrewBrain address:** your company's name in CrewBrain, the part before
  `.crewbrain.com`. The full address works too.
- **Username** and **password:** your CrewBrain login.

The password is used once, to get an access token from CrewBrain, and is not
stored. The token stays valid while it is in use. If CrewBrain stops
accepting it, Home Assistant asks you to sign in again.

**Options:** how many days of jobs to show ahead, the time category a
clock-in is booked into, and the activity text it writes.

## Entities

| Entity | |
|---|---|
| `switch` Punch clock | On while you are clocked in. On clocks in, off clocks out. |
| `binary_sensor` Clocked in | The same, read-only. |
| `sensor` Clocked in since | When the running record started. |
| `sensor` Worked today | Minutes booked today. |
| `sensor` Next job | The next job that is not over yet, with the ones after it as attributes. |
| `calendar` Jobs | Your jobs. |

## Actions

```yaml
action: crewbrain.clock_in
data:
  time: "2026-10-19 08:00:00"   # optional, default now
  note: "Site visit"            # optional, the record's activity
response_variable: result       # {record_id, start, end, minutes}
```

```yaml
action: crewbrain.clock_out
data:
  time: "2026-10-19 16:30:00"   # optional, default now
response_variable: result
```

With more than one account set up, add `config_entry_id`.

## How it behaves

- Every clock-in and clock-out reads CrewBrain first. Clocking in while a
  record is running, or out while none is, is refused. So is anything when
  two records are open; fix those in CrewBrain.
- A clock-in is booked as working time without a job.
- The clocked-in state is read every minute, jobs every 30 minutes.

## Tests

`tests/e2e` runs the integration in a real Home Assistant against a stand-in
CrewBrain. It never reaches the real one.

```
docker compose -f tests/e2e/docker-compose.yml up --abort-on-container-exit --exit-code-from runner
docker compose -f tests/e2e/docker-compose.yml down -v
```
