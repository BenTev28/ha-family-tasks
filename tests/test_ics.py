"""Unit tests for the lossless editor and task edits (no server needed)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.family_tasks.ics import ICalDocument, fold
from custom_components.family_tasks.task import Task, TaskEditor, new_task_ical

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 11, 23, 20, 0, tzinfo=UTC)
SAMPLE = (Path(__file__).parent / "fixtures" / "tasks_org_sample.ics").read_text(newline="")
MANAGED = ["Hannah", "Rachel", "Owen", "Micah"]


def lines(text: str) -> list[str]:
    return text.split("\r\n")


def edit(raw: str, fn) -> tuple[str, TaskEditor]:
    doc = ICalDocument.parse(raw)
    editor = TaskEditor(doc, TZ)
    fn(editor)
    return doc.serialize(), editor


def test_roundtrip_is_byte_identical() -> None:
    assert ICalDocument.parse(SAMPLE).serialize() == SAMPLE


def test_roundtrip_lf_input_becomes_crlf() -> None:
    # calendar-data inside XML arrives with LF line endings
    assert ICalDocument.parse(SAMPLE.replace("\r\n", "\n")).serialize() == SAMPLE


def test_parse_sample() -> None:
    task = Task.from_ical("/x.ics", '"e1"', SAMPLE, TZ)
    assert task.uid == "7340213419562178372"
    assert task.summary.startswith("Take the recycling bins out to the curb and bring the empty ones back in")
    assert task.description == "Blue bin = paper, cans\nGreen bin = yard waste"
    assert task.categories == ["Groceries", "owen"]
    assert task.due == datetime(2026, 11, 25, 17, 0, tzinfo=TZ)
    assert task.parent_uid == "4093487710209877712"
    assert task.has_alarm
    assert task.priority == 5
    assert not task.completed


def test_assign_keeps_other_tags_and_every_other_line() -> None:
    out, editor = edit(SAMPLE, lambda e: e.set_assignee_tags(["Owen", "Micah"], MANAGED))
    assert editor.changed
    before, after = lines(SAMPLE), lines(out)
    assert [ln for ln in after if ln.startswith("CATEGORIES")] == ["CATEGORIES:Groceries,Owen,Micah"]
    # nothing else changed (same lines, same order)
    assert [ln for ln in before if not ln.startswith("CATEGORIES")] == [
        ln for ln in after if not ln.startswith("CATEGORIES")
    ]


def test_reassign_micah_to_owen_keeps_groceries() -> None:
    raw = SAMPLE.replace("CATEGORIES:Groceries,owen", "CATEGORIES:Micah,Groceries")
    out, _ = edit(raw, lambda e: e.set_assignee_tags(["Owen"], MANAGED))
    assert "CATEGORIES:Groceries,Owen\r\n" in out


def test_unassign_everyone_keeps_other_tags() -> None:
    out, _ = edit(SAMPLE, lambda e: e.set_assignee_tags([], MANAGED))
    assert "CATEGORIES:Groceries\r\n" in out


def test_unassign_removes_categories_when_empty() -> None:
    raw = SAMPLE.replace("CATEGORIES:Groceries,owen", "CATEGORIES:Owen")
    out, _ = edit(raw, lambda e: e.set_assignee_tags([], MANAGED))
    assert "CATEGORIES" not in out


def test_multiple_categories_lines_are_merged() -> None:
    raw = SAMPLE.replace("CATEGORIES:Groceries,owen", "CATEGORIES:Groceries\r\nCATEGORIES:Rachel")
    task = Task.from_ical("/x", None, raw, TZ)
    assert task.categories == ["Groceries", "Rachel"]
    out, _ = edit(raw, lambda e: e.set_assignee_tags(["Rachel", "Hannah"], MANAGED))
    assert out.count("CATEGORIES") == 1
    assert "CATEGORIES:Groceries,Rachel,Hannah\r\n" in out


def test_same_assignees_is_noop() -> None:
    raw = SAMPLE.replace("CATEGORIES:Groceries,owen", "CATEGORIES:Groceries,Owen")
    out, editor = edit(raw, lambda e: e.set_assignee_tags(["Owen"], MANAGED))
    assert not editor.changed
    assert out == raw


def test_edit_summary_keeps_alarms_subtask_link_and_x_props() -> None:
    out, _ = edit(SAMPLE, lambda e: (e.set_summary("Bins"), e.touch(NOW)))
    for needle in (
        "RELATED-TO:4093487710209877712",
        "X-APPLE-SORT-ORDER:722199000",
        "X-MOZ-LASTACK:20261120T183000Z",
        "X-TASKS-ORG-CUSTOM:keep-me",
        "TRIGGER;RELATED=END:PT0S",
        "RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=WE",
        "BEGIN:VTIMEZONE",
    ):
        assert needle in out
    assert out.count("BEGIN:VALARM") == 2
    assert "SUMMARY:Bins\r\n" in out
    assert "SEQUENCE:1\r\n" in out
    assert "LAST-MODIFIED:20261123T200000Z" in out


def test_text_escaping_roundtrip() -> None:
    out, _ = edit(SAMPLE, lambda e: e.set_description("a, b; c\\d\nline2"))
    assert "DESCRIPTION:a\\, b\\; c\\\\d\\nline2" in out
    assert Task.from_ical("/x", None, out, TZ).description == "a, b; c\\d\nline2"


def test_long_line_is_folded() -> None:
    folded = fold("SUMMARY:" + "é" * 80)
    for part in folded.split("\r\n"):
        assert len(part.encode()) <= 75


def test_complete_non_recurring() -> None:
    raw = SAMPLE.replace("RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=WE\r\n", "")
    out, _ = edit(raw, lambda e: e.complete(NOW))
    task = Task.from_ical("/x", None, out, TZ)
    assert task.completed
    assert "COMPLETED:20261123T200000Z" in out
    assert "PERCENT-COMPLETE:100" in out


def test_complete_recurring_advances_like_tasks_org() -> None:
    out, editor = edit(SAMPLE, lambda e: setattr(e, "result", e.complete(NOW)))
    assert editor.result is True  # type: ignore[attr-defined]
    task = Task.from_ical("/x", None, out, TZ)
    assert not task.completed
    assert "STATUS:" not in out.split("BEGIN:VALARM")[0]  # STATUS removed, like Tasks.org
    assert "COMPLETED:" not in out
    assert task.due == datetime(2026, 12, 2, 17, 0, tzinfo=TZ)
    assert task.dtstart == datetime(2026, 12, 2, 16, 0, tzinfo=TZ)
    assert "RRULE:FREQ=WEEKLY;COUNT=2;BYDAY=WE" in out
    # absolute alarm moved by a week, relative alarm untouched
    assert "TRIGGER;VALUE=DATE-TIME:20261202T230000Z" in out
    assert "TRIGGER;RELATED=END:PT0S" in out
    # TZID kept
    assert "DUE;TZID=America/Los_Angeles:20261202T170000" in out


def test_complete_recurring_count_one_completes() -> None:
    raw = SAMPLE.replace("COUNT=3", "COUNT=1")
    out, editor = edit(raw, lambda e: setattr(e, "result", e.complete(NOW)))
    assert editor.result is False  # type: ignore[attr-defined]
    assert Task.from_ical("/x", None, out, TZ).completed


def test_complete_recurring_until_exhausted_completes() -> None:
    raw = SAMPLE.replace("COUNT=3", "UNTIL=20261130T000000Z")
    out, _ = edit(raw, lambda e: e.complete(NOW))
    assert Task.from_ical("/x", None, out, TZ).completed


def test_complete_recurring_date_only_daily() -> None:
    raw = (
        SAMPLE.replace("DUE;TZID=America/Los_Angeles:20261125T170000", "DUE;VALUE=DATE:20261125")
        .replace("DTSTART;TZID=America/Los_Angeles:20261125T160000\r\n", "")
        .replace("RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=WE", "RRULE:FREQ=DAILY;INTERVAL=2")
    )
    out, _ = edit(raw, lambda e: e.complete(NOW))
    task = Task.from_ical("/x", None, out, TZ)
    assert task.due == date(2026, 11, 27)
    assert "DUE;VALUE=DATE:20261127" in out
    assert "RRULE:FREQ=DAILY;INTERVAL=2" in out


def test_reopen() -> None:
    raw = SAMPLE.replace("RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=WE\r\n", "")
    done, _ = edit(raw, lambda e: e.complete(NOW))
    out, _ = edit(done, lambda e: e.reopen())
    task = Task.from_ical("/x", None, out, TZ)
    assert not task.completed
    assert "PERCENT-COMPLETE" not in out


def test_new_task_due_datetime_adds_vtimezone() -> None:
    raw = new_task_ical("ABC", "New", NOW, "-//test//EN")
    out, _ = edit(raw, lambda e: e.set_due(datetime(2026, 11, 24, 17, 0, tzinfo=TZ)))
    assert "DUE;TZID=America/Los_Angeles:20261124T170000" in out
    assert "BEGIN:VTIMEZONE" in out and "TZID:America/Los_Angeles" in out
    assert Task.from_ical("/x", None, out, TZ).due == datetime(2026, 11, 24, 17, 0, tzinfo=TZ)


def test_set_due_date_only() -> None:
    out, _ = edit(SAMPLE, lambda e: e.set_due(date(2026, 11, 30)))
    assert "DUE;VALUE=DATE:20261130" in out
    # DTSTART converted to a date so value types match (RFC 5545)
    assert "DTSTART;VALUE=DATE:20261125" in out


def test_clear_due() -> None:
    out, _ = edit(SAMPLE, lambda e: e.set_due(None))
    assert "\r\nDUE" not in out


@pytest.mark.parametrize(("value", "label"), [(1, "high"), (4, "high"), (5, "medium"), (9, "low"), (0, None)])
def test_priority_labels(value: int, label: str | None) -> None:
    from custom_components.family_tasks.task import priority_label

    assert priority_label(value) == label
