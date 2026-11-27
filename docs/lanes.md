# lanes: one declaration, one clock model, one freshness verdict

Design record for `edge_platform.lanes` (`spec.py`, `freshness.py`, `gate.py`). The module
docstrings are the authority for behaviour; this file holds the reasons and what was ruled out.

## Purpose

A lane is one scheduled producer of rows. Every lane is declared once, and one function says whether
its output is fresh. A dead lane pages; a quiet period does not.

## Why one model

Before v0.11 three cadence models existed and disagreed: a `market_gated: bool` nothing in the
package connected to a clock, a consumer-side conversion of a session limit into wall hours (with a
fallback to wall hours when the conversion failed), and a content-source cadence with no silence
limit at all. One model replaced them: a lane names its clock and states its limit in that clock's
hours.

## Shape

- `LaneSpec(... cadence_h, due_h, clock, repo, ...)`: `cadence_h` is how often it is SCHEDULED,
  `due_h` its silence limit, both in hours of `clock` (`"wall"` or a name the consumer registered).
  A consumer extends it by subclassing and calls `super().__post_init__()`.
- `Clock` protocol: `name`, `hours(start, end) -> float | None`, the hours of THIS clock elapsed in
  the span; `None` means the clock cannot say. A session clock counts session time only.
  `register_clock(clock)`; `get_clock(name)` raises `UnknownClock`.
- `Observation(newest_output, last_run, last_status, runs, registered_at, paused, held_since, held)`:
  what the consumer read, in datetimes, not hours.
- `judge(spec, obs, *, now) -> Judged(state, clock, limit_h, age_h, silence_h, why)`: the ONLY entry
  point a consumer calls. `verdict(...)` stays as the pure core it calls.
- `SourceGate.input_absent` calls `judge`, so the gate and the page cannot disagree.

Limit rule: `limit_h = spec.due_h` when set; otherwise `min(ttl*3, ttl+24)` wall hours with a 36h
floor. A lane on any clock but wall MUST set `due_h`; registration refuses it otherwise, because the
36h floor is a wall-clock idea.

## Invariants

1. **Cadence, due and age are in the lane's own clock.** A 2-minute session lane is
   `cadence_h=1/30, due_h=0.25, clock="<session clock>"`. Overnight adds zero hours, so the lane is
   `ok` at 03:00 and `STOPPED` 15 session-minutes after the open if it has not run.
2. **A clock that cannot answer makes the lane `unknown`, never `ok`.** There is no fallback to wall
   hours: that fallback is exactly the false STOPPED (or false ok) the model exists to prevent.
3. **The states, exactly:**
   - `ok`: newest output within `limit_h`.
   - `STALE`: runs are happening and succeeding, output is older than the limit (it runs and
     produces nothing).
   - `STOPPED`: no finished run within the limit (the run ledger is silent).
   - `NEVER_RAN`: enabled, scheduled, `runs == 0`, and registered longer than one limit ago. Before
     that it is `idle`. (Calling never-ran `idle` forever was a guard that could not fire for a lane
     nobody wired.)
   - `unknown`: running but unmeasurable (no freshness column, unreadable table, clock `None`).
   - `paused`, `held`, `HELD_LONG`, `abstaining`, `idle`: operator dials and declared declines.
   `FINDING_STATES` (`STALE`, `STOPPED`, `NEVER_RAN`, `unknown`, `HELD_LONG`) are the ones that page.
4. **`STOPPED` needs a run ledger.** A judged lane declares `work_unit` or `heartbeat`, or
   registration refuses it; only `transport="computed"` is judged on output age alone.
5. **Names a spec cites are checked at registration** against the consumer's `LaneVocab`; an empty
   list refuses every reference. An unknown name is a finding, never a pass.

## Failure semantics

`judge` never raises on data: it returns `unknown` with a `why`. It raises only for a clock that is
not registered, which registration already prevents. `SourceGate`'s switch read is best-effort by
construction: an unreadable switch table reads as nothing disabled (a control table must never black
out the pipeline), never silently: a warning always, and `on_degraded(reason)` when supplied.

## Telemetry

| signal | where | what should watch it |
|---|---|---|
| one `Judged` per lane per sweep | the consumer's watchdog state | pages on `FINDING_STATES`; the sweep itself is a lane with a heartbeat, watched by an external staleness watcher |
| a per-lane state gauge labelled with the real clock name | the consumer's metrics exporter | a dashboard |
| `on_degraded` from `SourceGate` | the consumer's push path | the gate fails open there by design; the degraded event is the page |

## Acceptance tests (`tests/lanes/`)

1. `judge` over every state, including: a session lane at 03:00 on a Sunday with output from Friday's
   close -> `ok`; the same lane on Monday 20 minutes after the open with no run since Friday ->
   `STOPPED`.
2. Must-fire both ways for `NEVER_RAN`: registered 2 limits ago with 0 runs -> finding; registered
   half a limit ago -> `idle`.
3. A clock returning `None` -> `unknown`, not the wall fallback.
4. A non-wall lane without `due_h` is refused at registration; `due_h <= cadence_h` is refused.
5. `SourceGate.input_absent` on a session lane overnight returns not-absent.

## Ruled out

| option | why |
|---|---|
| a boolean `market_gated` | says a clock exists, not which, and nothing enforced its use |
| converting a session limit to wall hours | wrong across half-days and holidays, and needs a fallback |
| a long wall cadence on a short lane | a dead 2-minute lane would page after days |
| "ran ok" as the freshness test | not-due and did-it are the same success |
| Dagster freshness policies / asset checks | need the Dagster runtime; the scheduler here is a separate service |
