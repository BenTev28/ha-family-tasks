# 0001: Google Tasks stores assignees as a marker line in the notes

- **Status:** accepted (2026-11-24)
- **Applies to:** `backends/google_tasks`

## Context

Google Tasks (API v1) has no tag, label or category field. It has no hidden metadata either: nothing like Calendar's `extendedProperties`, and `links` is read-only. Its `assignmentInfo` is read-only and covers only Docs or Chat assignments. Whole lists can't be shared between Google accounts, so a family typically uses one shared account and phones read the same list.

The integration's contract requires assignees on every service (`docs/ARCHITECTURE.md` §1). On Google Tasks they could be stored as:

1. **A marker line in `notes`.** Visible and editable in the Google Tasks apps, so assignments show up on phones.
2. **Home Assistant only (emulated).** Invisible to phones, and lost if the integration's storage is lost.

## Decision

Use option 1. The capability is `assignees = native`, because the data lives on the service and other clients see it.

### Format

The marker is the **last non-empty line** of `notes`:

```
Pick up the parcel from the post office.

Assigned to: Owen, Micah
```

- **Writing:** `Assigned to: ` + the assignee tags in the configured case, joined by `, `. It is separated from the rest of the notes by one blank line. With no assignees, the line (and the blank line before it) is removed.
- **Reading:**
  - The prefix is case-insensitive. `Assigned to:`, `Assigned:` and `Assignees:` are all accepted, so a person typing on a phone doesn't need the exact form.
  - Names are split on commas and trimmed, then matched to configured tags case-insensitively.
  - Names that aren't configured tags are reported as `other_tags`, so nothing is lost. They are kept as typed when the line is rewritten.
- **Notes in the contract:** the task's `description` is the notes *without* the marker line, so the card, `get_tasks` and `todo` entities never show it twice. Writing `description` keeps the marker, and writing assignees keeps the rest of the notes exactly as they were.
- **Limits:** the marker counts toward the 8,192-character `notes` limit. A write that would exceed it fails with a clear error and is never truncated silently.

## Consequences

- People see, and can change, assignments in the Google Tasks app by editing the last line of the notes.
- A typo there (for example `Assigned to: Oewn`) shows up as an unknown tag in `other_tags` instead of being silently dropped.
- Other integrations or apps reading the notes will see the marker line. This is acceptable for a family list.
- Priority and recurrence remain `emulated` on Google Tasks (`docs/ARCHITECTURE.md` §4). This decision covers assignees only.
