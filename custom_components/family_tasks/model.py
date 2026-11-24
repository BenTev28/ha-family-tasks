"""Service-neutral task model.

Everything outside ``backends/`` works only with these types.  A backend maps
them onto its service; nothing service-specific may leak through here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Final


class _Unset:
    """Sentinel: 'leave this field alone' (distinct from None = 'clear it')."""

    _instance: _Unset | None = None

    def __new__(cls) -> _Unset:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET: Final = _Unset()


class Priority(StrEnum):
    """Neutral priority levels (backends map to their own scales)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class Task:
    """One task as the integration sees it, whatever the service."""

    id: str  # stable id from the service (CalDAV UID, Graph id, ...)
    list_id: str
    summary: str
    description: str | None = None
    completed: bool = False
    completed_at: datetime | None = None
    due: date | datetime | None = None
    tags: list[str] = field(default_factory=list)  # assignee tags + any other tags
    priority: Priority | None = None
    rrule: str | None = None  # RFC 5545 RRULE value, e.g. "FREQ=WEEKLY;BYDAY=MO"
    parent_id: str | None = None
    has_alarm: bool = False
    version: str | None = None  # opaque change marker (ETag etc.); changes on every edit
    updated: datetime | None = None

    def is_overdue(self, now: datetime) -> bool:
        if self.completed or self.due is None:
            return False
        if isinstance(self.due, datetime):
            return self.due < now
        return self.due < now.date()

    def due_date(self, tz: Any) -> date | None:
        if self.due is None:
            return None
        if isinstance(self.due, datetime):
            return self.due.astimezone(tz).date()
        return self.due


@dataclass
class AssigneeChange:
    """How to change the assignee tags of a task."""

    replace: list[str] | None = None  # set exactly these (None = keep current)
    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)

    def resolve(self, current_tags: list[str], managed: list[str]) -> list[str]:
        """Return the wanted assignee tags, in the configured case."""
        by_lc = {m.lower(): m for m in managed}
        wanted = (
            list(self.replace)
            if self.replace is not None
            else [by_lc[t.lower()] for t in current_tags if t.lower() in by_lc]
        )
        for tag in self.add:
            if tag.lower() not in {w.lower() for w in wanted}:
                wanted.append(tag)
        drop = {t.lower() for t in self.remove}
        return list(dict.fromkeys(w for w in wanted if w.lower() not in drop))


@dataclass
class TaskChange:
    """A partial edit. UNSET fields are left alone; None clears a field."""

    summary: str | _Unset = UNSET
    description: str | _Unset | None = UNSET
    due: date | datetime | _Unset | None = UNSET
    priority: Priority | _Unset | None = UNSET
    rrule: str | _Unset | None = UNSET
    assignees: AssigneeChange | None = None
    completed: bool | None = None  # True = complete (recurring tasks advance), False = reopen

    def touched(self) -> set[str]:
        """Names of the fields this change sets (for capability checks)."""
        names = {
            name for name in ("summary", "description", "due", "priority", "rrule") if getattr(self, name) is not UNSET
        }
        if self.assignees is not None:
            names.add("assignees")
        if self.completed is not None:
            names.add("completed")
        return names


@dataclass
class WriteContext:
    """What a backend needs to know from the integration to apply a change."""

    managed_tags: list[str]  # every configured assignee tag
    tz: Any  # HA time zone (tzinfo)
    now: datetime
