# Architecture

Family Tasks gives Home Assistant one family task list with **assignees**, whatever service stores the tasks. The integration is split into a **fixed public contract** and **service adapters** ("backends").

```
  card ─┐   actions ─┐   events ─┐   todo / sensor / binary_sensor entities
        └────────────┴───────────┴──────────────┐
                         core (service-neutral)
          model.py · coordinator.py · services.py · websocket_api.py
                                   │  TaskBackend interface (backends/base.py)
          ┌───────────────┬────────┴──────┬────────────────┐
        caldav        microsoft_todo   google_tasks     ha_todo
       (done)          (planned)        (planned)       (planned)
```

## 1. The public contract (identical for every service)

These are the integration's interface. A backend must **never** add, remove or rename any of them. Changes are additive only, and they apply to all backends at once.

| Surface | Contract |
|---|---|
| Entities | `todo.<list>` (one per selected list), `sensor.<tag>_open_family_tasks`, `binary_sensor.<tag>_has_overdue_family_tasks` |
| Actions | `family_tasks.add_task`, `update_task`, `complete_task`, `reopen_task`, `delete_task`, `get_tasks`, `refresh`, with the fields and responses in `services.yaml` |
| Events | `family_tasks_task_added`, `family_tasks_task_completed` (with `recurring`), `family_tasks_task_assigned` (with `persons`, `tags`), fired for changes from any client |
| Websocket | `family_tasks/config` → people, lists (each with `service` and `capabilities`), `completed_days`; `family_tasks/subscribe` → task snapshots |
| Task object | `uid, list, summary, description, status, due, due_has_time, completed, overdue, assignees, assignee_tags, other_tags, priority, rrule, etag, has_alarm, parent_uid` (`services.task_to_dict`) |
| Card | `custom:family-tasks-card` and its config keys |
| Options | assignee mapping (person → tag → colour), lists, poll interval, completed-task visibility |

What differs between services is expressed **only** through **capabilities** (section 3). It is never expressed through different fields, actions or events.

## 2. The backend interface

`backends/base.py` defines it.

**`TaskBackend`** is one connected account:

| Method | Meaning |
|---|---|
| `async_list_lists()` | The lists the account can see (`ListInfo(id, name)`). |
| `async_fetch(list_id)` | The full current snapshot `{task_id: Task}`. The backend may sync incrementally and keeps its own cursors. |
| `async_create(list_id, summary, change, ctx)` | Create a task, then apply a `TaskChange`. Returns the new id. |
| `async_update(list_id, task_id, change, ctx)` | Apply a partial `TaskChange`. It must not overwrite concurrent edits silently. Completing a recurring task follows the backend's documented rule. |
| `async_delete(list_id, task_id)` | Delete the task. |
| `capabilities` | A `Capabilities`, see section 3. |

**`BackendSpec`** describes how the backend is set up:
- `key`, `name`, `capabilities`, `redact` (keys to hide in diagnostics), `placeholders`.
- `credentials_schema()`, `async_validate()`, `unique_id()`, `title()`, `create()`.
- The connect step is `connect_<key>`, so each backend has its own form texts. Add an `async_step_connect_<key>` to the config flow when you add a backend.
- OAuth backends will extend this with HA's `config_entry_oauth2_flow` (section 5).

**Neutral types** live in `model.py`:
- `Task`: `id, list_id, summary, description, completed, completed_at, due, tags, priority, rrule, parent_id, has_alarm, version, updated`.
- `TaskChange`: `UNSET` leaves a field alone and `None` clears it. Assignees are changed through an `AssigneeChange` (replace, add or remove), and `completed=True/False` completes or reopens.
- `WriteContext`: the managed assignee tags, the time zone and now.

**Errors:** `BackendAuthError` (triggers reauth), `BackendConnectionError`, `ConflictError`, `TaskNotFoundError`, `BackendError`. Messages must be safe to log.

## 3. Capabilities

Each feature is one of:
- `native`: stored on the service, so other apps see it.
- `emulated`: kept by the integration in HA storage, so it works in HA but other apps don't see it.
- `none`: not available.

| | `assignees` | `priority` | `recurrence` | `description` | `due_time` | `subtasks` | `conflict_detection` |
|---|---|---|---|---|---|---|---|
| Meaning | who a task is for | high/medium/low | RRULE repeat | notes | due can have a time of day | `parent_uid` reported | concurrent edits detected |

How each part of the integration uses them:
- **Card:** hides or disables controls for `none` features and the time picker when `due_time` is false. It marks `emulated` features as "Home Assistant only".
- **Actions:** setting a `none` field raises `unsupported_field` with the service name, and is never silently dropped.
- **todo entity:** advertises only the HA to-do features the backend supports (due time, notes).

## 4. Service mappings

Facts were researched in Nov 2026. Items marked ⚠ must be confirmed against the live service before that backend ships.

| Feature | CalDAV (Nextcloud, Radicale, Fastmail, iCloud…) | Microsoft To Do (Graph) | Google Tasks (API v1) | HA to-do entity (Local To-do, …) |
|---|---|---|---|---|
| Lists | collections with VTODO (PROPFIND discovery) | `/me/todo/lists` (shared lists via `Tasks.ReadWrite.Shared`) | `tasklists` (lists can't be shared between accounts) | any `todo.*` entity the user picks |
| **Assignees** | native: `CATEGORIES` | native: `categories` ⚠ (Outlook category names; check that arbitrary values are accepted and the app shows them) | **decision needed**: marker line in `notes` (visible on phones, editable) or emulated | emulated |
| Priority | native: `PRIORITY` 1/5/9 | native: `importance` high/normal/low (medium = normal) | emulated | emulated |
| Recurrence | native: `RRULE`; completing advances in place (Tasks.org rule) | native: `patternedRecurrence` (map the RRULE subset; ⚠ the service may spawn a new task on completion) | none via API (recurring tasks are invisible as such) → emulated with the core engine | emulated with the core engine |
| Notes | `DESCRIPTION` | `body` (text) | `notes` (minus any marker) | `description` |
| Due time | yes (`TZID`) | date-only ⚠ (time could go to `reminderDateTime`) | date-only | yes |
| Subtasks | `RELATED-TO` | `checklistItems` (a different model, not reported) | `parent` | no |
| Conflicts | `If-Match` ETag, re-apply once | `@odata.etag` + `If-Match` ⚠; else compare `lastModifiedDateTime` before writing | `etag` ⚠; else compare `updated` before writing | in-process (entity actions) |
| Change detection | `sync-collection` → ctag → full fetch | `tasks/delta` per list (`410` → full resync) | `tasks.list?updatedMin&showDeleted&showHidden` (paginate) | entity state listener |
| Auth | URL + user + app password | OAuth2 (user's Entra app, `Tasks.ReadWrite(.Shared) offline_access`) | OAuth2 (user's Google Cloud client, `tasks` scope) | none |
| Limits | — | ~10k requests / 10 min per mailbox, 429 + Retry-After | 50k queries/day per client ID | — |

## 5. Shared building blocks (planned)

- **Sidecar store** (`emulated` features): an HA `Store` per config entry, keyed by `(list_id, task_id)`. It holds assignee tags, priority and the RRULE for services that can't store them. Entries are pruned when the task disappears. The core reads and writes it, so backends with emulated features don't each re-implement it.
- **Recurrence engine:** the next-occurrence logic in `backends/caldav/vtodo.py` (COUNT/UNTIL, date vs date-time, DST-safe) moves to a neutral `recurrence.py`. CalDAV keeps using it for in-place advance, and emulated backends use it to move the due date on completion.
- **OAuth:** each OAuth backend registers its own implementation (`family_tasks_microsoft`, `family_tasks_google`) with HA's `config_entry_oauth2_flow`. Users bring their own client ID and secret, as with HA core's Google and OneDrive integrations. Tokens live in the config entry and are refreshed by HA.
- **Contract tests:** one parametrized suite (`tests/contract/`) runs the same scenarios against every backend, using fakes for cloud services and Radicale for CalDAV:
  - add with two assignees
  - reassign keeping other tags
  - complete or reopen
  - recurring complete
  - delete
  - remote change → event
  - conflict
  - unsupported field → error

## 6. Adding a backend (checklist)

1. Create `backends/<key>/__init__.py` with a `TaskBackend` and a `BackendSpec`. Register it in `backends/__init__.py`.
2. Declare `capabilities` honestly. Prefer `native`, then `emulated`, then `none`.
3. Add `async_step_connect_<key>` to the config flow and `config.step.connect_<key>` texts to `strings.json` (and `translations/en.json`).
4. Map fields only in the backend. Don't touch `services.py`, `websocket_api.py`, the entities or the card, except for additive, all-backend changes agreed in section 1.
5. Keep untouched data intact: never drop properties or tags the integration doesn't manage.
6. Detect conflicts, or document the residual last-write-wins window.
7. No credentials, tokens or account URLs in logs at INFO or above, or in diagnostics. List them in `redact`.
8. Pass the contract test suite. Add a row to the mapping table above, a `docs/services/<key>.md` page, and a row in the README services table.
