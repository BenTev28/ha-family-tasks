"""family_tasks.* actions. Identical for every backend."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from dateutil.rrule import rrulestr
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import DOMAIN
from .coordinator import FamilyTasksCoordinator
from .model import UNSET, AssigneeChange, Priority, Task, TaskChange

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
REFRESH_SCHEMA = vol.Schema({vol.Optional("list"): cv.entity_domain("todo")})
GET_SCHEMA = vol.Schema(
    {
        vol.Optional("list"): cv.entity_domain("todo"),
        vol.Optional("assignee"): vol.Any(cv.entity_domain("person"), vol.In(["unassigned"])),
        vol.Optional("status", default="open"): vol.In(STATUSES),
        vol.Optional("due"): vol.In(DUE_FILTERS),
    }
)


def coordinators(hass: HomeAssistant) -> list[FamilyTasksCoordinator]:
    return [e.runtime_data for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]


def _for_list(hass: HomeAssistant, entity_id: str) -> tuple[FamilyTasksCoordinator, str]:
    for coordinator in coordinators(hass):
        list_id = coordinator.list_for_entity(entity_id)
        if list_id is not None and list_id in coordinator.lists:
            return coordinator, list_id
    raise ServiceValidationError(
        translation_domain=DOMAIN, translation_key="unknown_list", translation_placeholders={"list": entity_id}
    )


def _for_uid(hass: HomeAssistant, uid: str) -> FamilyTasksCoordinator:
    for coordinator in coordinators(hass):
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


def _due(data: dict[str, Any]) -> date | datetime | object | None:
    if "due_datetime" in data:
        value = data["due_datetime"]
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=dt_util.get_default_time_zone())
        return value
    if "due_date" in data:
        return data["due_date"]
    return UNSET


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


def _priority(value: str | None) -> Priority | None:
    return Priority(value) if value and value != "none" else None


def _build_change(coordinator: FamilyTasksCoordinator, data: dict[str, Any]) -> TaskChange:
    """Map action data to a neutral TaskChange and check it against the backend's capabilities."""
    assignees: AssigneeChange | None = None
    if "assignees" in data or data.get("add_assignees") or data.get("remove_assignees"):
        assignees = AssigneeChange(
            replace=_tags(coordinator, data["assignees"]) if "assignees" in data else None,
            add=_tags(coordinator, data.get("add_assignees", [])),
            remove=_tags(coordinator, data.get("remove_assignees", [])),
        )
    change = TaskChange(
        summary=data.get("summary", UNSET),
        description=(data["description"] or None) if "description" in data else UNSET,
        due=_due(data),  # type: ignore[arg-type]
        priority=_priority(data["priority"]) if "priority" in data else UNSET,
        rrule=_rrule(data["rrule"]) if "rrule" in data else UNSET,
        assignees=assignees,
    )
    if isinstance(change.due, datetime) and not coordinator.capabilities.due_time:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unsupported_field",
            translation_placeholders={"fields": "due_datetime", "service": coordinator.spec.name},
        )
    if unsupported := coordinator.unsupported(change):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unsupported_field",
            translation_placeholders={"fields": ", ".join(unsupported), "service": coordinator.spec.name},
        )
    return change


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register actions (once, independent of config entries)."""

    async def add_task(call: ServiceCall) -> ServiceResponse:
        coordinator, list_id = _for_list(hass, call.data["list"])
        change = _build_change(coordinator, {k: v for k, v in call.data.items() if k != "summary"})
        uid = await coordinator.async_create(list_id, call.data["summary"], change)
        return {"uid": uid}

    async def update_task(call: ServiceCall) -> ServiceResponse:
        coordinator = _for_uid(hass, call.data["uid"])
        changed = await coordinator.async_update(call.data["uid"], _build_change(coordinator, dict(call.data)))
        return {"uid": call.data["uid"], "changed": changed}

    async def complete_task(call: ServiceCall) -> ServiceResponse:
        coordinator = _for_uid(hass, call.data["uid"])
        changed = await coordinator.async_update(call.data["uid"], TaskChange(completed=True))
        return {"uid": call.data["uid"], "changed": changed}

    async def reopen_task(call: ServiceCall) -> ServiceResponse:
        coordinator = _for_uid(hass, call.data["uid"])
        changed = await coordinator.async_update(call.data["uid"], TaskChange(completed=False))
        return {"uid": call.data["uid"], "changed": changed}

    async def delete_task(call: ServiceCall) -> ServiceResponse:
        coordinator = _for_uid(hass, call.data["uid"])
        await coordinator.async_delete(call.data["uid"])
        return {"uid": call.data["uid"]}

    async def get_tasks(call: ServiceCall) -> ServiceResponse:
        return {"tasks": query_tasks(hass, **call.data)}

    async def refresh(call: ServiceCall) -> None:
        # Fetch now instead of waiting for the next poll. A list refreshes the
        # whole account it belongs to (one coordinator polls all its lists).
        targets = [_for_list(hass, call.data["list"])[0]] if "list" in call.data else coordinators(hass)
        for coordinator in targets:
            await coordinator.async_refresh()
            if not coordinator.last_update_success:
                # The cause is logged by the coordinator; don't echo it here, as
                # backend errors can carry server URLs.
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="refresh_failed")

    for name, handler, schema, response in (
        ("add_task", add_task, ADD_SCHEMA, SupportsResponse.OPTIONAL),
        ("update_task", update_task, UPDATE_SCHEMA, SupportsResponse.OPTIONAL),
        ("complete_task", complete_task, UID_SCHEMA, SupportsResponse.OPTIONAL),
        ("reopen_task", reopen_task, UID_SCHEMA, SupportsResponse.OPTIONAL),
        ("delete_task", delete_task, UID_SCHEMA, SupportsResponse.OPTIONAL),
        ("get_tasks", get_tasks, GET_SCHEMA, SupportsResponse.ONLY),
        ("refresh", refresh, REFRESH_SCHEMA, SupportsResponse.NONE),
    ):
        hass.services.async_register(DOMAIN, name, handler, schema=schema, supports_response=response)


def task_to_dict(task: Task, *, list_entity: str | None, coordinator: FamilyTasksCoordinator, now: datetime) -> dict:
    """The task object of the public contract (actions + websocket). Same for every backend."""
    tag_to_person = coordinator.tag_to_person
    assignee_tags = [t for t in task.tags if t.lower() in tag_to_person]
    due: str | None = None
    if isinstance(task.due, datetime):
        due = task.due.astimezone(now.tzinfo).isoformat()
    elif task.due is not None:
        due = task.due.isoformat()
    return {
        "uid": task.id,
        "list": list_entity,
        "summary": task.summary,
        "description": task.description,
        "status": "completed" if task.completed else "needs_action",
        "due": due,
        "due_has_time": isinstance(task.due, datetime),
        "completed": task.completed_at.isoformat() if task.completed_at else None,
        "overdue": task.is_overdue(now),
        "assignees": coordinator.assignees_of(task),
        "assignee_tags": assignee_tags,
        "other_tags": [t for t in task.tags if t.lower() not in tag_to_person],
        "priority": task.priority.value if task.priority else None,
        "rrule": task.rrule,
        "etag": task.version,
        "has_alarm": task.has_alarm,
        "parent_uid": task.parent_id,
    }


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
    for coordinator in coordinators(hass):
        for list_id, state in coordinator.lists.items():
            entity_id = coordinator.list_entity_id(list_id)
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
                    day = task.due_date(now.tzinfo)
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
                out.append(task_to_dict(task, list_entity=entity_id, coordinator=coordinator, now=now))
    out.sort(key=lambda t: (t["status"] == "completed", t["due"] is None, t["due"] or "", t["summary"].lower()))
    return out
