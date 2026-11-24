"""family_tasks.* actions."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from dateutil.rrule import rrulestr
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import DOMAIN
from .coordinator import FamilyTasksCoordinator
from .task import PRIORITY_TO_ICAL, TaskEditor, task_to_dict
from .todo import async_apply_update

PRIORITIES = ["high", "medium", "low", "none"]
STATUSES = ["open", "completed", "all"]
DUE_FILTERS = ["overdue", "today", "today_or_overdue", "week"]

_FIELDS = {
    vol.Optional("assignees"): vol.All(cv.ensure_list, [cv.entity_domain("person")]),
    vol.Exclusive("due_date", "due"): vol.Any(None, cv.date),
    vol.Exclusive("due_datetime", "due"): vol.Any(None, cv.datetime),
    vol.Optional("description"): vol.Any(None, cv.string),
    vol.Optional("priority"): vol.Any(None, vol.In(PRIORITIES)),
    vol.Optional("rrule"): vol.Any(None, cv.string),
}

ADD_SCHEMA = vol.Schema(
    {
        vol.Required("list"): cv.entity_domain("todo"),
        vol.Required("summary"): vol.All(cv.string, vol.Length(min=1)),
        **_FIELDS,
    }
)
UPDATE_SCHEMA = vol.Schema(
    {
        vol.Required("uid"): cv.string,
        vol.Optional("summary"): vol.All(cv.string, vol.Length(min=1)),
        vol.Optional("add_assignees"): vol.All(cv.ensure_list, [cv.entity_domain("person")]),
        vol.Optional("remove_assignees"): vol.All(cv.ensure_list, [cv.entity_domain("person")]),
        **_FIELDS,
    }
)
UID_SCHEMA = vol.Schema({vol.Required("uid"): cv.string})
GET_SCHEMA = vol.Schema(
    {
        vol.Optional("list"): cv.entity_domain("todo"),
        vol.Optional("assignee"): vol.Any(cv.entity_domain("person"), vol.In(["unassigned"])),
        vol.Optional("status", default="open"): vol.In(STATUSES),
        vol.Optional("due"): vol.In(DUE_FILTERS),
    }
)


def _coordinators(hass: HomeAssistant) -> list[FamilyTasksCoordinator]:
    return [e.runtime_data for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]


def _for_list(hass: HomeAssistant, entity_id: str) -> tuple[FamilyTasksCoordinator, str]:
    for coordinator in _coordinators(hass):
        path = coordinator.path_for_entity(entity_id)
        if path is not None and path in coordinator.lists:
            return coordinator, path
    raise ServiceValidationError(
        translation_domain=DOMAIN, translation_key="unknown_list", translation_placeholders={"list": entity_id}
    )


def _for_uid(hass: HomeAssistant, uid: str) -> FamilyTasksCoordinator:
    for coordinator in _coordinators(hass):
        if coordinator.find(uid):
            return coordinator
    raise ServiceValidationError(
        translation_domain=DOMAIN, translation_key="unknown_task", translation_placeholders={"uid": uid}
    )


def _tags(coordinator: FamilyTasksCoordinator, persons: list[str]) -> list[str]:
    mapping = coordinator.person_to_tag
    unknown = [p for p in persons if p not in mapping]
    if unknown:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_person",
            translation_placeholders={"persons": ", ".join(unknown), "known": ", ".join(mapping)},
        )
    return [mapping[p] for p in dict.fromkeys(persons)]


def _due(data: dict[str, Any]) -> tuple[bool, date | datetime | None]:
    if "due_datetime" in data:
        value = data["due_datetime"]
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=dt_util.get_default_time_zone())
        return True, value
    if "due_date" in data:
        return True, data["due_date"]
    return False, None


def _rrule(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().removeprefix("RRULE:")
    try:
        if not value.upper().startswith("FREQ=") and ";FREQ=" not in value.upper():
            raise ValueError
        rrulestr(value, dtstart=datetime(2026, 1, 1))
    except (ValueError, TypeError):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_rrule", translation_placeholders={"rrule": value}
        ) from None
    return value


def _priority(value: str | None) -> int | None:
    return PRIORITY_TO_ICAL.get(value) if value else None


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register actions (once, independent of config entries)."""

    async def add_task(call: ServiceCall) -> ServiceResponse:
        data = call.data
        coordinator, path = _for_list(hass, data["list"])
        tags = _tags(coordinator, data.get("assignees", []))
        has_due, due = _due(data)
        rrule = _rrule(data.get("rrule"))

        def mutate(editor: TaskEditor) -> None:
            if has_due and due is not None:
                editor.set_due(due)
            if data.get("description"):
                editor.set_description(data["description"])
            if data.get("priority"):
                editor.set_priority(_priority(data["priority"]))
            if rrule:
                editor.set_rrule(rrule)
            if tags:
                editor.set_assignee_tags(tags, coordinator.managed_tags)

        uid = await coordinator.async_create(path, data["summary"], mutate)
        return {"uid": uid}

    async def update_task(call: ServiceCall) -> ServiceResponse:
        data = call.data
        coordinator = _for_uid(hass, data["uid"])
        has_due, due = _due(data)
        changed = await async_apply_update(
            coordinator,
            data["uid"],
            summary=data.get("summary"),
            due=due,
            set_due=has_due,
            description=data.get("description"),
            set_description="description" in data,
            priority=_priority(data.get("priority")),
            set_priority="priority" in data,
            rrule=_rrule(data.get("rrule")),
            set_rrule="rrule" in data,
            assignee_tags=_tags(coordinator, data["assignees"]) if "assignees" in data else None,
            add_tags=_tags(coordinator, data.get("add_assignees", [])),
            remove_tags=_tags(coordinator, data.get("remove_assignees", [])),
        )
        return {"uid": data["uid"], "changed": changed}

    async def complete_task(call: ServiceCall) -> ServiceResponse:
        coordinator = _for_uid(hass, call.data["uid"])
        changed = await async_apply_update(coordinator, call.data["uid"], completed=True)
        return {"uid": call.data["uid"], "changed": changed}

    async def reopen_task(call: ServiceCall) -> ServiceResponse:
        coordinator = _for_uid(hass, call.data["uid"])
        changed = await async_apply_update(coordinator, call.data["uid"], completed=False)
        return {"uid": call.data["uid"], "changed": changed}

    async def delete_task(call: ServiceCall) -> ServiceResponse:
        coordinator = _for_uid(hass, call.data["uid"])
        await coordinator.async_delete(call.data["uid"])
        return {"uid": call.data["uid"]}

    async def get_tasks(call: ServiceCall) -> ServiceResponse:
        return {"tasks": query_tasks(hass, **call.data)}

    for name, handler, schema, response in (
        ("add_task", add_task, ADD_SCHEMA, SupportsResponse.OPTIONAL),
        ("update_task", update_task, UPDATE_SCHEMA, SupportsResponse.OPTIONAL),
        ("complete_task", complete_task, UID_SCHEMA, SupportsResponse.OPTIONAL),
        ("reopen_task", reopen_task, UID_SCHEMA, SupportsResponse.OPTIONAL),
        ("delete_task", delete_task, UID_SCHEMA, SupportsResponse.OPTIONAL),
        ("get_tasks", get_tasks, GET_SCHEMA, SupportsResponse.ONLY),
    ):
        hass.services.async_register(DOMAIN, name, handler, schema=schema, supports_response=response)


def query_tasks(
    hass: HomeAssistant,
    *,
    list: str | None = None,
    assignee: str | None = None,
    status: str = "open",
    due: str | None = None,
    visible_only: bool = False,
) -> list[dict[str, Any]]:
    """Filter tasks across all loaded entries; also used by the websocket API."""
    now = dt_util.now()
    today = now.date()
    out: list[dict[str, Any]] = []
    for coordinator in _coordinators(hass):
        for path, state in coordinator.lists.items():
            entity_id = coordinator.list_entity_id(path)
            if list is not None and entity_id != list:
                continue
            for task in state.tasks.values():
                if status == "open" and task.completed:
                    continue
                if status == "completed" and not task.completed:
                    continue
                if visible_only and not coordinator.visible(task, now):
                    continue
                people = coordinator.assignees_of(task)
                if assignee == "unassigned" and people:
                    continue
                if assignee not in (None, "unassigned") and assignee not in people:
                    continue
                if due is not None:
                    day = task.due_date(now.tzinfo)  # type: ignore[arg-type]
                    if day is None:
                        continue
                    overdue = task.is_overdue(now)
                    if due == "overdue" and not overdue:
                        continue
                    if due == "today" and day != today:
                        continue
                    if due == "today_or_overdue" and not (overdue or day == today):
                        continue
                    if due == "week" and not (today <= day <= today + timedelta(days=7)):
                        continue
                out.append(
                    task_to_dict(
                        task,
                        list_entity=entity_id,
                        tag_to_person=coordinator.tag_to_person,
                        now=now,
                    )
                )
    out.sort(key=lambda t: (t["status"] == "completed", t["due"] is None, t["due"] or "", t["summary"].lower()))
    return out
