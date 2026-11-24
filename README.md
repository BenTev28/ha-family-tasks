# Family Tasks for Home Assistant

A custom integration that turns a shared **Nextcloud (CalDAV) task list** into a family to-do list with **assignees**. It ships with a touch-first Lovelace card for a wall display.

Assignees are stored as VTODO `CATEGORIES` (`Hannah`, `Rachel`, `Owen`, `Micah`), so Nextcloud stays the single source of truth. An assignment made in Home Assistant, Tasks.org (DAVx5), Nextcloud Tasks or by Claude shows up everywhere. Phones keep their "My tasks" filters (personal list OR tasks tagged with my name) and need no changes.

## What you get

| Thing | Details |
|---|---|
| `todo.<list>` | One per selected list. Create, edit, complete, delete, due date or datetime, notes. Repeating tasks advance the way Tasks.org does it (see below). |
| `sensor.<person>_open_family_tasks` | Count of open tasks tagged with that person. Attributes: `tasks`, `overdue`. |
| `binary_sensor.<person>_has_overdue_family_tasks` | On when an open task for that person is overdue. Attribute: `tasks`. |
| `family_tasks.*` actions | `add_task`, `update_task`, `complete_task`, `reopen_task`, `delete_task`, `get_tasks` (assignees, tags, priority, repeat, uid, etag). |
| Events | `family_tasks_task_added`, `family_tasks_task_completed`, `family_tasks_task_assigned`. These fire for changes made anywhere, detected while polling. |
| `custom:family-tasks-card` | Wall-display card: filter by person, add and edit dialogs with big person chips (colour + avatar), repeat, priority. Registered automatically. |

## Install

**HACS (custom repository):** HACS → ⋮ → Custom repositories → add this repo as type *Integration*. A private repo needs HACS to have a GitHub token that can read it. Install, then restart Home Assistant.

**Manual:** copy `custom_components/family_tasks` into `<config>/custom_components/` and restart.

Then go to **Settings → Devices & services → Add integration → Family Tasks**:

1. **CalDAV URL:** `https://cloud.example.com/remote.php/dav`. It must be `https://` and must not contain a username or password.
2. **Username:** the account that owns the shared list (for example `household`).
3. **App password:** create one in Nextcloud under Settings → Security → Devices & sessions. Type it into this form only; it is never logged.
4. Pick the task lists to expose. *Family To-Do* is preselected.

To rotate the app password later, open the integration's ⋮ menu → **Reconfigure**. Entity IDs stay the same.

### Options

- **People:** person entity → tag → optional card colour (`#rrggbb`). By default it uses every `person.*` entity, with the first word of its name as the tag. Tags are matched case-insensitively and written in the configured case.
- **Task lists**, **poll interval** (default 60 s, minimum 30 s), and **show completed tasks for N days** (default 7; 0 hides them immediately).

## Actions

```yaml
action: family_tasks.add_task
data:
  list: todo.family_to_do
  summary: Feed the cat
  assignees: [person.owen, person.micah]
  due_date: "2026-11-30"          # or due_datetime: "2026-11-30 17:00:00"
  priority: high                  # high | medium | low  (iCal 1 / 5 / 9)
  rrule: FREQ=WEEKLY;BYDAY=MO,TH  # optional
response_variable: created        # -> {uid: ...}
```

`update_task` takes `uid` plus any of the fields above. For people it takes `assignees` (replace all), `add_assignees` or `remove_assignees`. Setting `due_date: null` clears the due date, `description: ""` clears the notes, `priority: none` clears the priority, and `rrule: ""` stops repeating.

`get_tasks` filters by `list`, `assignee` (a person or `unassigned`), `status` (`open` / `completed` / `all`) and `due` (`overdue` / `today` / `today_or_overdue` / `week`).

An unknown person makes the action fail with a clear error; it is never written as a stray tag.

### Events

```yaml
trigger:
  - trigger: event
    event_type: family_tasks_task_assigned
action:
  - action: notify.notify
    data:
      message: "New task for {{ trigger.event.data.tags | join(', ') }}: {{ trigger.event.data.summary }}"
```

| Event | Data |
|---|---|
| `family_tasks_task_added` | `uid`, `summary`, `list`, `assignees` |
| `family_tasks_task_completed` | the same, plus `recurring` (true when a repeating task was advanced instead of closed) |
| `family_tasks_task_assigned` | the same, plus `persons` and `tags` (only the people *added*) |

Changes are found by diffing each poll against the previous one. Changes made while Home Assistant was down produce no events. The first load after a restart is a baseline and fires nothing.

## Card

```yaml
type: custom:family-tasks-card
entity: todo.family_to_do
title: Family tasks      # optional
sort: due                # due | person
show_completed: true
default_filter: everyone # everyone | unassigned | <tag>
```

It has a visual editor and follows the active HA theme. Person chips use the colour from the options plus the person entity's picture.

## How data is kept safe

- **Read-modify-write on the raw iCalendar.** The editor (`ics.py`) keeps every content line exactly as received and replaces only the properties being edited. `X-` properties, `RELATED-TO` (subtasks), `VALARM`, `RRULE`, unknown tags and so on are untouched. Every write bumps `DTSTAMP`, `LAST-MODIFIED` and `SEQUENCE`.
  - One caveat: calendar data arrives inside a WebDAV XML response, and XML parsing turns CRLF into LF. Output therefore always uses CRLF, as RFC 5545 requires. Apart from line endings, untouched lines are identical.
- **Conflict-safe writes:** `PUT` uses `If-Match: <etag>`. On `412` the task is re-fetched, the same change is applied again and the write is retried once. If it fails again, the action raises "changed elsewhere at the same time" rather than overwriting.
- **Change detection:** WebDAV `sync-collection` (sync tokens). If that fails it falls back to the collection ctag and a full fetch. The integration polls on the configured interval and refreshes straight after its own writes.
- **Dates:** times are written with `TZID` (the HA time zone, America/Los_Angeles). A `VTIMEZONE` is added if the task lacks one. Date-only due dates use `VALUE=DATE`, and `DTSTART` is kept to the same value type.
- **Credentials:** the app password is used only in an `Authorization` header. Nothing logs URLs or credentials at INFO or above. Debug logs show collection *paths* only, and diagnostics redact the URL, username and password. The config form rejects URLs that contain credentials.

## Repeating tasks

This was checked against the Tasks.org source (`RepeatTaskHelper.kt`, `iCalendar.kt`, Nov 2026). Completing a task that has an `RRULE` **advances the same VTODO in place**. No completed copy is created.

- `DUE` moves to the next occurrence, counted from the old due date (from now if there was none). The time of day and `TZID` / `VALUE=DATE` style are kept.
- `DTSTART` and any absolute (`VALUE=DATE-TIME`) alarm triggers move by the same amount. Relative alarms are unchanged.
- `COUNT` is decremented.
- `COMPLETED`, `STATUS` and `PERCENT-COMPLETE` are **removed**, as Tasks.org does.
- With `COUNT=1`, or when `UNTIL` is passed, the task is completed normally.
- The `family_tasks_task_completed` event fires with `recurring: true`.

This matters because Tasks.org **does not** advance a task that another client marked `STATUS:COMPLETED`; it shows it as completed for good. So Home Assistant has to advance it itself.

Tasks.org's "repeat after completion" option is a local, per-phone setting that is never synced over CalDAV. Home Assistant can't see it and always repeats from the due date.

Nextcloud Tasks (web) also advances the task in place and decrements `COUNT`.

## Not supported (by design)

- **Reordering (`todo.move_item`).** The card and phones sort by due date or person. Nextcloud's `X-APPLE-SORT-ORDER` isn't kept consistently across clients, so a manual order in HA would be misleading.
- Personal lists, the shopping list and calendars are out of scope.

## Why not the `caldav` library?

The integration talks WebDAV directly through Home Assistant's own aiohttp session. It needs exact control of ETags, `sync-collection` and raw bodies. It also avoids the known trap where a custom integration pins `caldav` and breaks the built-in CalDAV integration. The only requirement is `icalendar>=6.1.0`, used to generate `VTIMEZONE`. It's a minimum version, so HA's own copy is used.

## Development

```bash
./scripts_test.sh            # ruff + full test suite (Python 3.14 container + throwaway Radicale)
./scripts_test.sh tests/test_ics.py -k recurring
docker run --rm -v "$PWD:/github/workspace" ghcr.io/home-assistant/hassfest
```

The tests cover these areas:
- Lossless round-trip of a Tasks.org-style VTODO (`tests/fixtures/tasks_org_sample.ics`: X- props, two alarms, subtask link, RRULE).
- Adding and removing assignees while keeping other tags.
- ETag conflicts, both merged and persistent.
- Recurrence: COUNT, UNTIL and date-only rules.
- Sync-token deletes, sensors and events.
- Config, options and reconfigure flows.
- No credentials in logs or diagnostics.

> The fixture is **synthetic**, modelled on Tasks.org's output. Before relying on it, replace it with a real export of a phone-created task that has a subtask and an alarm (Nextcloud web → task → ⋮ → Download), then re-run the tests.
