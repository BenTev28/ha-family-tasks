# CalDAV (Nextcloud, Radicale, Fastmail, iCloud, Baïkal, …)

## Setup

| Field | What to enter |
|---|---|
| Server URL | The CalDAV root. For Nextcloud this is `https://<your-server>/remote.php/dav`. It must start with `https://` and must not contain a username or password. |
| Username | The account that owns the shared list. A dedicated household account that shares the list with everyone works well. |
| Password | An **app password**, where your service offers one: Nextcloud under *Settings → Security*; iCloud and Fastmail in their account security settings. It is never logged. |

After you connect, pick the lists to show. A single list, or lists with "family" in their name, are preselected.

**Phones:** assignees are stored as task tags (`CATEGORIES`). In Tasks.org (with DAVx5), a filter such as *"my list OR tag = Owen"* gives each person a "My tasks" view that includes everything assigned to them.

## What can be stored

| Feature | Support |
|---|---|
| Assignees | ✅ tags on the task, visible to other apps |
| Priority | ✅ `PRIORITY` 1 / 5 / 9 (high / medium / low, the Tasks.org convention) |
| Repeat | ✅ `RRULE` (see below) |
| Due time | ✅ |
| Subtasks | shown as subtasks (`RELATED-TO`) |
| Simultaneous edits | ✅ detected (see below) |

## How your data is protected

- **Only edited lines change.** Tasks are edited line by line in their raw iCalendar form, and everything Family Tasks didn't change is kept exactly as it was. That includes other apps' private properties, subtask links, reminders, repeat rules and tags.
  - Every edit updates `DTSTAMP`, `LAST-MODIFIED` and `SEQUENCE`, as the standard requires.
  - One caveat: line endings are always written as CRLF, the standard form.
- **Simultaneous edits aren't lost.** Writes use `If-Match` with the task's ETag. If a phone changed the task in the meantime, Family Tasks re-reads it, applies your change on top and tries once more. If that also collides, the action fails with "changed elsewhere at the same time" rather than overwriting.
- **Efficient sync.** Family Tasks uses WebDAV `sync-collection` (sync tokens), and falls back to the collection's ctag. It refreshes immediately after its own changes.
- **Dates.**
  - Times use your Home Assistant time zone (`TZID`), with a `VTIMEZONE` added when needed.
  - All-day due dates stay date-only (`VALUE=DATE`).
- **Credentials.** The password is sent only in the `Authorization` header. Logs and diagnostics never contain it, the server URL or list paths. The setup form rejects URLs that contain credentials.

## Repeating tasks

Family Tasks does exactly what Tasks.org does (checked against its source code, November 2026). Completing a repeating task **moves the same task to its next date**; no completed copy is created.

- **The task moves:** the due date moves to the next occurrence, counted from the old due date (or from now if there was none). The start date and any fixed-time reminders move by the same amount.
- **Counted repeats** (`COUNT`) count down. When none are left, or the `UNTIL` date has passed, the task is completed normally.
- **Not marked completed:** the completed markers are removed rather than set, matching Tasks.org.
- **The event:** `family_tasks_task_completed` fires with `recurring: true`.

This matters because Tasks.org **won't** advance a repeating task that another app marked completed; it just shows it as done forever.

Tasks.org's "repeat after completion" setting is stored only on each phone, and is never synced, so Home Assistant always repeats from the due date.

## Technical notes

Family Tasks talks WebDAV directly through Home Assistant's own HTTP client, instead of using the `caldav` Python library. There are two reasons:
- It needs exact control over ETags, sync tokens and raw task data.
- It avoids a known problem where a custom integration that pins `caldav` breaks Home Assistant's built-in CalDAV integration.

Its only dependency is `icalendar>=6.1.0`, which Home Assistant already ships.
