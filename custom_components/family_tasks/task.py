"""Task model: read VTODO values and apply targeted edits."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

from .ics import Component, ContentLine, ICalDocument, escape_text, split_list, unescape_text

PRIORITY_TO_ICAL = {"high": 1, "medium": 5, "low": 9}


def priority_label(value: int | None) -> str | None:
    """Map iCal PRIORITY (1-9) to high/medium/low the way Tasks.org does."""
    if not value:
        return None
    if value <= 4:
        return "high"
    if value == 5:
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# date helpers


def _zone(name: str | None, default: tzinfo) -> tzinfo:
    if not name:
        return default
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return default


def parse_dt(line: ContentLine | None, local_tz: tzinfo) -> date | datetime | None:
    """Parse DUE/DTSTART/COMPLETED style values; floating times become local."""
    if line is None:
        return None
    value = line.value.strip()
    params = line.params
    try:
        if params.get("VALUE", "").upper() == "DATE" or (len(value) == 8 and value.isdigit()):
            return datetime.strptime(value[:8], "%Y%m%d").date()
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        naive = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
        return naive.replace(tzinfo=_zone(params.get("TZID"), local_tz))
    except ValueError:
        return None


def format_dt(value: date | datetime, local_tz: tzinfo, like: ContentLine | None = None) -> tuple[str, dict[str, str]]:
    """Format a value, copying the style (UTC / TZID / floating) of ``like``."""
    if not isinstance(value, datetime):
        return value.strftime("%Y%m%d"), {"VALUE": "DATE"}
    if like is not None and not isinstance(parse_dt(like, local_tz), datetime):
        like = None  # previous value was a date; fall through to TZID style
    if like is not None and like.value.strip().endswith("Z"):
        return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"), {}
    tzid = (like.params.get("TZID") if like is not None else None) or str(local_tz)
    zone = _zone(tzid, local_tz)
    return value.astimezone(zone).strftime("%Y%m%dT%H%M%S"), {"TZID": tzid}


def utc_stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# read model


@dataclass
class Task:
    """A parsed view of one VTODO resource."""

    href: str
    etag: str | None
    raw: str
    uid: str
    summary: str = ""
    description: str | None = None
    completed: bool = False
    completed_at: datetime | None = None
    due: date | datetime | None = None
    dtstart: date | datetime | None = None
    categories: list[str] = field(default_factory=list)
    priority: int | None = None
    rrule: str | None = None
    has_alarm: bool = False
    parent_uid: str | None = None
    last_modified: datetime | None = None

    @classmethod
    def from_ical(cls, href: str, etag: str | None, raw: str, local_tz: tzinfo) -> Task:
        doc = ICalDocument.parse(raw)
        todo = doc.vtodo
        uid_line = todo.first("UID")
        summary = todo.first("SUMMARY")
        desc = todo.first("DESCRIPTION")
        status = todo.first("STATUS").value.strip().upper() if todo.first("STATUS") else ""
        completed_line = todo.first("COMPLETED")
        completed_at = parse_dt(completed_line, local_tz)
        categories: list[str] = []
        for line in todo.lines("CATEGORIES"):
            categories.extend(split_list(line.value))
        prio_line = todo.first("PRIORITY")
        try:
            priority = int(prio_line.value) if prio_line else None
        except ValueError:
            priority = None
        parent = None
        for rel in todo.lines("RELATED-TO"):
            if rel.params.get("RELTYPE", "PARENT").upper() == "PARENT":
                parent = rel.value.strip()
                break
        rrule_line = todo.first("RRULE")
        last_mod = parse_dt(todo.first("LAST-MODIFIED"), UTC)
        return cls(
            href=href,
            etag=etag,
            raw=raw,
            uid=uid_line.value.strip() if uid_line else href,
            summary=unescape_text(summary.value) if summary else "",
            description=unescape_text(desc.value) if desc else None,
            completed=status == "COMPLETED" or (completed_line is not None and status != "NEEDS-ACTION"),
            completed_at=completed_at if isinstance(completed_at, datetime) else None,
            due=parse_dt(todo.first("DUE"), local_tz),
            dtstart=parse_dt(todo.first("DTSTART"), local_tz),
            categories=categories,
            priority=priority,
            rrule=rrule_line.value.strip() if rrule_line else None,
            has_alarm=bool(todo.components("VALARM")),
            parent_uid=parent,
            last_modified=last_mod if isinstance(last_mod, datetime) else None,
        )

    def is_overdue(self, now: datetime) -> bool:
        if self.completed or self.due is None:
            return False
        if isinstance(self.due, datetime):
            return self.due < now
        return self.due < now.date()

    def due_date(self, local_tz: tzinfo) -> date | None:
        if self.due is None:
            return None
        if isinstance(self.due, datetime):
            return self.due.astimezone(local_tz).date()
        return self.due


# ---------------------------------------------------------------------------
# edits (operate on an ICalDocument; return True when something changed)


class TaskEditor:
    """Apply targeted edits to the master VTODO of a document."""

    def __init__(self, doc: ICalDocument, local_tz: tzinfo) -> None:
        self.doc = doc
        self.todo: Component = doc.vtodo
        self.tz = local_tz
        self.changed = False

    def _mark(self, changed: bool) -> None:
        self.changed = self.changed or changed

    # simple properties -------------------------------------------------
    def set_summary(self, summary: str) -> None:
        current = self.todo.first("SUMMARY")
        if current is not None and unescape_text(current.value) == summary:
            return
        self._mark(self.todo.set("SUMMARY", escape_text(summary)))

    def set_description(self, description: str | None) -> None:
        current = self.todo.first("DESCRIPTION")
        if not description:
            self._mark(self.todo.remove("DESCRIPTION"))
            return
        if current is not None and unescape_text(current.value) == description:
            return
        self._mark(self.todo.set("DESCRIPTION", escape_text(description)))

    def set_priority(self, priority: int | None) -> None:
        current = self.todo.first("PRIORITY")
        if not priority:
            self._mark(self.todo.remove("PRIORITY"))
            return
        if current is not None and current.value.strip() == str(priority):
            return
        self._mark(self.todo.set("PRIORITY", str(priority)))

    def set_rrule(self, rrule: str | None) -> None:
        if not rrule:
            self._mark(self.todo.remove("RRULE"))
            return
        rrule = rrule.removeprefix("RRULE:").strip()
        current = self.todo.first("RRULE")
        if current is not None and current.value.strip() == rrule:
            return
        self._mark(self.todo.set("RRULE", rrule))

    # due / start ---------------------------------------------------------
    def set_due(self, due: date | datetime | None) -> None:
        current_line = self.todo.first("DUE")
        current = parse_dt(current_line, self.tz)
        if due is None:
            self._mark(self.todo.remove("DUE"))
            return
        if type(current) is type(due) and current == due:
            return
        value, params = format_dt(due, self.tz, current_line)
        if "TZID" in params:
            self.doc.ensure_timezone(params["TZID"])
        self._mark(self.todo.set("DUE", value, params))
        self._fix_dtstart(due)

    def _fix_dtstart(self, due: date | datetime) -> None:
        """Keep DTSTART valid (same value type, not after DUE)."""
        line = self.todo.first("DTSTART")
        start = parse_dt(line, self.tz)
        if start is None:
            return
        if isinstance(due, datetime) != isinstance(start, datetime):
            # RFC 5545 requires matching value types
            if isinstance(due, datetime):
                start = datetime.combine(start, due.timetz())
            else:
                start = start.astimezone(self.tz).date()  # type: ignore[union-attr]
        if start > due:  # type: ignore[operator]
            start = due
        value, params = format_dt(start, self.tz, line)
        self._mark(self.todo.set("DTSTART", value, params))

    # categories ------------------------------------------------------------
    def set_assignee_tags(self, wanted: list[str], managed: list[str]) -> None:
        """Make the managed (assignee) tags equal ``wanted``; keep all others.

        ``managed`` is every configured assignee tag; matching is
        case-insensitive, writing uses the configured case.
        """
        managed_lc = {m.lower() for m in managed}
        wanted_lc = {w.lower() for w in wanted}
        current: list[str] = []
        for line in self.todo.lines("CATEGORIES"):
            current.extend(split_list(line.value))
        current_assignees = {c.lower() for c in current if c.lower() in managed_lc}
        if current_assignees == wanted_lc and all(c in wanted for c in current if c.lower() in managed_lc):
            return
        result: list[str] = []
        seen: set[str] = set()
        for tag in current:
            key = tag.lower()
            if key in seen:
                continue
            if key in managed_lc:
                if key not in wanted_lc:
                    continue
                tag = next(w for w in wanted if w.lower() == key)
            result.append(tag)
            seen.add(key)
        for tag in wanted:
            if tag.lower() not in seen:
                result.append(tag)
                seen.add(tag.lower())
        new_lines = [ContentLine.build("CATEGORIES", ",".join(escape_text(t) for t in result))] if result else []
        self.todo.replace_all("CATEGORIES", new_lines)
        self._mark(True)

    # completion ------------------------------------------------------------
    def complete(self, now: datetime) -> bool:
        """Complete the task. Returns True if a recurring task was advanced.

        Mirrors Tasks.org (RepeatTaskHelper + iCalendar.applyLocal): a
        recurring task is advanced in place - DUE moves to the next
        occurrence, DTSTART and absolute alarms move by the same amount,
        COUNT is decremented, and COMPLETED/STATUS/PERCENT-COMPLETE are
        removed.  When the series is exhausted the task is completed.
        """
        if self.todo.first("RRULE") is not None and self._advance(now):
            return True
        self._mark(self.todo.set("STATUS", "COMPLETED"))
        self._mark(self.todo.set("COMPLETED", utc_stamp(now)))
        self._mark(self.todo.set("PERCENT-COMPLETE", "100"))
        return False

    def reopen(self) -> None:
        self._mark(self.todo.remove("COMPLETED"))
        self._mark(self.todo.remove("PERCENT-COMPLETE"))
        self._mark(self.todo.set("STATUS", "NEEDS-ACTION"))

    def _advance(self, now: datetime) -> bool:
        rrule_line = self.todo.first("RRULE")
        assert rrule_line is not None
        parts = dict(p.split("=", 1) for p in rrule_line.value.strip().split(";") if "=" in p)
        parts = {k.upper(): v for k, v in parts.items()}
        count = int(parts["COUNT"]) if parts.get("COUNT", "").isdigit() else None
        if count is not None and count <= 1:
            return False
        until_raw = parts.pop("UNTIL", None)
        parts.pop("COUNT", None)

        due_line = self.todo.first("DUE")
        old_due = parse_dt(due_line, self.tz)
        all_day = old_due is not None and not isinstance(old_due, datetime)
        if old_due is None:
            seed = now.astimezone(self.tz).replace(microsecond=0)
        elif all_day:
            seed = datetime.combine(old_due, datetime.min.time())
        else:
            seed = old_due  # type: ignore[assignment]
        rule = rrulestr(";".join(f"{k}={v}" for k, v in parts.items()), dtstart=seed)
        nxt = rule.after(seed, inc=False)
        if nxt is None:
            return False
        if until_raw:
            until = self._parse_until(until_raw, nxt)
            if until is not None and nxt > until:
                return False

        new_due: date | datetime = nxt.date() if all_day else nxt
        delta = (
            timedelta(days=(new_due - old_due).days)  # type: ignore[operator]
            if all_day
            else (nxt - seed)
        )
        value, params = format_dt(new_due, self.tz, due_line)
        self._mark(self.todo.set("DUE", value, params))

        start_line = self.todo.first("DTSTART")
        start = parse_dt(start_line, self.tz)
        if start is not None and old_due is not None:
            value, params = format_dt(start + delta, self.tz, start_line)  # type: ignore[operator]
            self._mark(self.todo.set("DTSTART", value, params))
        if old_due is not None:
            self._shift_absolute_alarms(delta)

        if count is not None:
            new_rule = ";".join(
                f"{k}={count - 1}" if k.upper() == "COUNT" else f"{k}={v}"
                for k, v in (p.split("=", 1) for p in rrule_line.value.strip().split(";") if "=" in p)
            )
            self._mark(self.todo.set("RRULE", new_rule))

        self._mark(self.todo.remove("COMPLETED"))
        self._mark(self.todo.remove("STATUS"))
        self._mark(self.todo.remove("PERCENT-COMPLETE"))
        return True

    def _parse_until(self, raw: str, like: datetime) -> datetime | None:
        try:
            if len(raw) == 8:
                day = datetime.strptime(raw, "%Y%m%d")
                end = day.replace(hour=23, minute=59, second=59)
                return end.replace(tzinfo=like.tzinfo) if like.tzinfo else end
            if raw.endswith("Z"):
                value = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
                return value if like.tzinfo else value.replace(tzinfo=None)
            value = datetime.strptime(raw[:15], "%Y%m%dT%H%M%S")
            return value.replace(tzinfo=like.tzinfo)
        except ValueError:
            return None

    def _shift_absolute_alarms(self, delta: timedelta) -> None:
        for alarm in self.todo.components("VALARM"):
            trig = alarm.first("TRIGGER")
            if trig is None or trig.params.get("VALUE", "").upper() != "DATE-TIME":
                continue
            when = parse_dt(trig, self.tz)
            if not isinstance(when, datetime):
                continue
            value, params = format_dt(when + delta, self.tz, trig)
            alarm.set("TRIGGER", value, {"VALUE": "DATE-TIME", **params})
            self._mark(True)

    # bookkeeping -------------------------------------------------------------
    def touch(self, now: datetime) -> None:
        """Bump DTSTAMP, LAST-MODIFIED and SEQUENCE (RFC 5545 3.8.7.4)."""
        stamp = utc_stamp(now)
        self.todo.set("DTSTAMP", stamp)
        self.todo.set("LAST-MODIFIED", stamp)
        seq = self.todo.first("SEQUENCE")
        try:
            current = int(seq.value) if seq else 0
        except ValueError:
            current = 0
        self.todo.set("SEQUENCE", str(current + 1))


def new_task_ical(uid: str, summary: str, now: datetime, prodid: str) -> str:
    """Skeleton VCALENDAR for a new task; fields are filled via TaskEditor."""
    stamp = utc_stamp(now)
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        f"PRODID:{prodid}\r\n"
        "BEGIN:VTODO\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{stamp}\r\n"
        f"CREATED:{stamp}\r\n"
        f"LAST-MODIFIED:{stamp}\r\n"
        f"SUMMARY:{escape_text(summary)}\r\n"
        "STATUS:NEEDS-ACTION\r\n"
        "SEQUENCE:0\r\n"
        "END:VTODO\r\n"
        "END:VCALENDAR\r\n"
    )


def task_to_dict(
    task: Task,
    *,
    list_entity: str | None,
    tag_to_person: dict[str, str],
    now: datetime,
) -> dict[str, Any]:
    """Serialise for websocket / service responses."""
    assignee_tags = [c for c in task.categories if c.lower() in tag_to_person]
    other = [c for c in task.categories if c.lower() not in tag_to_person]
    due: str | None = None
    if isinstance(task.due, datetime):
        due = task.due.astimezone(now.tzinfo).isoformat()
    elif task.due is not None:
        due = task.due.isoformat()
    return {
        "uid": task.uid,
        "list": list_entity,
        "summary": task.summary,
        "description": task.description,
        "status": "completed" if task.completed else "needs_action",
        "due": due,
        "due_has_time": isinstance(task.due, datetime),
        "completed": task.completed_at.isoformat() if task.completed_at else None,
        "overdue": task.is_overdue(now),
        "assignees": list(dict.fromkeys(tag_to_person[t.lower()] for t in assignee_tags)),
        "assignee_tags": assignee_tags,
        "other_tags": other,
        "priority": priority_label(task.priority),
        "rrule": task.rrule,
        "etag": task.etag,
        "has_alarm": task.has_alarm,
        "parent_uid": task.parent_uid,
    }
