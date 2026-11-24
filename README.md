# Family Tasks for Home Assistant

Shared family task lists with **assignees**, plus a touch-friendly card for a wall display.

Your tasks stay in the service you already use, so phones and other apps keep working. Family Tasks adds who-does-what on top. Everything in Home Assistant (entities, actions, events and the card) works the same whichever service a list lives on.

| Service | Status | Where assignees are stored |
|---|---|---|
| CalDAV: Nextcloud, Radicale, Fastmail, iCloud, Baïkal, … | ✅ available | task tags (`CATEGORIES`), visible in Tasks.org, Thunderbird, Nextcloud Tasks |
| Microsoft To Do | planned | task categories |
| Google Tasks | planned | a last line in the notes, e.g. `Assigned to: Owen, Micah` |
| Home Assistant to-do lists (Local To-do, …) | planned | Home Assistant only |

## What you get

- **A to-do entity per list.** Add, edit, complete and delete tasks, with due dates and notes. Repeating tasks move to their next date when completed.
- **Per-person sensors:** `sensor.<person>_open_family_tasks` and `binary_sensor.<person>_has_overdue_family_tasks`.
- **Actions** to add, update, complete, reopen, delete and query tasks, with assignees, priority and repeat.
- **Events** when a task is added, completed or assigned. These fire even when the change was made on a phone.
- **The `family-tasks-card`:** filter by person, big tappable person chips with colours and avatars, add and edit dialogs. It follows your theme.

## Install

1. In Home Assistant, open **HACS** (the **Marketplace** in 2026.11 and later) → ⋮ → **Custom repositories**. Add `https://github.com/BenTev28/ha-family-tasks` as type **Integration**, then install **Family Tasks**.
   Alternatively, copy `custom_components/family_tasks` into your `config/custom_components/` folder.
2. Restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Family Tasks**, pick your service, sign in, and choose the lists to show.
4. Add the card to a dashboard:

   ```yaml
   type: custom:family-tasks-card
   entity: todo.family_to_do
   ```

People are set up automatically from your `person.*` entities. To change tags or card colours, open the integration's **Configure** dialog. To rotate a password, use **Reconfigure**. Your entity IDs stay the same.

## Documentation

| | |
|---|---|
| [Using Family Tasks](docs/usage.md) | Options, actions, events, the card, and what's deliberately not supported |
| [CalDAV / Nextcloud](docs/services/caldav.md) | Setup tips, how your data is protected, and how repeating tasks behave with Tasks.org |
| [Architecture](docs/ARCHITECTURE.md) | How services plug in, and what each one can store |
| [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Code of conduct](CODE_OF_CONDUCT.md) | |

## Support

Found a bug or have an idea? [Open an issue](https://github.com/BenTev28/ha-family-tasks/issues/new/choose). Report security problems privately, as described in [SECURITY.md](SECURITY.md).

MIT licensed.
