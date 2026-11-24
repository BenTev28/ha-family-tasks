# Using Family Tasks

Everything on this page works the same for every task service. If a service can't store something (for example, a due *time* on a date-only service), the card hides that control. An action that tries to set it fails with a clear message instead of silently dropping it. To see what each service supports, check [the mapping table](ARCHITECTURE.md#4-service-mappings).

## Options

Open **Settings → Devices & services → Family Tasks → Configure**.

- **People:** each person entity gets a **tag** (the name stored on tasks) and an optional card **colour** (`#rrggbb`).
  - Default: every `person.*` entity, with the first word of its name as the tag.
  - Tags are matched case-insensitively and written in the case you configure.
- **Task lists:** which lists become to-do entities.
- **Poll interval:** default 60 s, minimum 30 s.
- **Show completed tasks for:** default 7 days. Set it to 0 to hide tasks as soon as they're done.

## Entities

| Entity | What it shows |
|---|---|
| `todo.<list>` | The list. Works with HA's standard to-do card and actions too. |
| `sensor.<tag>_open_family_tasks` | The number of open tasks for that person. Attributes: `tasks`, `overdue`. |
| `binary_sensor.<tag>_has_overdue_family_tasks` | On when that person has an overdue task. Attribute: `tasks`. |

## Actions

```yaml
action: family_tasks.add_task
data:
  list: todo.family_to_do
  summary: Feed the cat
  assignees: [person.owen, person.micah]
  due_date: "2026-11-30"          # or due_datetime: "2026-11-30 17:00:00"
  priority: high                  # high | medium | low
  rrule: FREQ=WEEKLY;BYDAY=MO,TH  # optional repeat rule
response_variable: created        # -> {uid: ...}
```

| Action | What it does |
|---|---|
| `add_task` | Creates a task. Returns its `uid`. |
| `update_task` | Changes only the fields you pass. For people, pass `assignees` (replaces everyone), `add_assignees` or `remove_assignees`. These clear a field: `due_date: null`, `description: ""`, `priority: none`, `rrule: ""`. |
| `complete_task` / `reopen_task` | Marks a task done or not done. Repeating tasks move to their next date instead of closing. |
| `delete_task` | Deletes the task. |
| `get_tasks` | Returns tasks, filtered by `list`, `assignee` (a person or `unassigned`), `status` (`open` / `completed` / `all`) and `due` (`overdue` / `today` / `today_or_overdue` / `week`). |

Each returned task includes:
- `uid`, `summary`, `description`, `status`, `due`, `overdue`
- `assignees` (person entities), `assignee_tags`, `other_tags`
- `priority`, `rrule`, `parent_uid`

Assigning someone who isn't configured as a person fails with a clear error. It is never written to the task as a stray tag.

## Events

| Event | Data |
|---|---|
| `family_tasks_task_added` | `uid`, `summary`, `list`, `assignees` |
| `family_tasks_task_completed` | the same, plus `recurring` (true when a repeating task moved to its next date) |
| `family_tasks_task_assigned` | the same, plus `persons` and `tags`: only the people who were *added* |

Events fire for changes made anywhere, including other apps and phones. Changes are picked up at the next poll.

Two cases fire nothing:
- Changes made while Home Assistant was off.
- The first load after a restart.

Example: tell people when they're given a task.

```yaml
trigger:
  - trigger: event
    event_type: family_tasks_task_assigned
action:
  - action: notify.notify
    data:
      message: "New task for {{ trigger.event.data.tags | join(', ') }}: {{ trigger.event.data.summary }}"
```

## The card

```yaml
type: custom:family-tasks-card
entity: todo.family_to_do
title: Family tasks      # optional
sort: due                # due | person
show_completed: true
default_filter: everyone # everyone | unassigned | <tag>
```

The card has a visual editor, and it follows your theme in light and dark mode. Person chips use the colour from the options and the person's picture. Fields that a service only stores in Home Assistant, not on the service itself, are labelled "Home Assistant only".

## Not supported, by design

- **Reordering** (`todo.move_item`). The card and phone apps sort by due date or person, and services keep manual order inconsistently, so a manual order in HA would be misleading.
- **Personal lists, shopping lists and calendars** are out of scope. Use HA's built-in integrations for those.
