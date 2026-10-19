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
| `binary_sensor` Status | Clocked in or clocked out, read-only. |
| `sensor` Clocked in since | The clock-in time, or "Not clocked in". The exact moment is in the `since` attribute. |
| `sensor` Worked today | Hours and minutes booked today. |
| `sensor` Next job | The next job that is not over yet: your role, the crew, remarks and notes, and the jobs after it as attributes. |
| `calendar` Jobs | Your jobs, with the same details. On while one is running. |
| `sensor` Vacation days left | This year's remaining vacation days. The rest of the vacation account is in its attributes. |
| `calendar` Absences | Your vacation, sick and other days off. On while one lasts. |

Vacation and absences appear only if your company lets your login read them.

## Requesting time off

In Home Assistant's calendar, add an event to **Absences**:

- **Summary:** the type, as CrewBrain names it, for example `Vacation`. The
  calendar's `types` attribute lists the ones you can ask for.
- **Description:** the reason, optional.
- **All day** on, and the first and last day.

The request goes to CrewBrain and shows in the calendar as not approved yet.
Deleting it withdraws it, as long as it is not approved. Approving stays
with your company.

A stand-in, if you want one on every request, is set in the options. It
starts as the one CrewBrain has for you. In automations, use
`calendar.create_event` on the Absences calendar.

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
