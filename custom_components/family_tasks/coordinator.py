"""Coordinator: polls a backend and applies writes. Service-neutral."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .backends import CONF_BACKEND, get_spec
from .backends.base import (
    BackendAuthError,
    BackendError,
    Capabilities,
    ConflictError,
    Support,
    TaskBackend,
    TaskNotFoundError,
)
from .const import (
    ATTR_COLOR,
    ATTR_PERSON,
    ATTR_TAG,
    CONF_ASSIGNEES,
    CONF_COMPLETED_DAYS,
    CONF_LISTS,
    CONF_SCAN_INTERVAL,
    DEFAULT_COMPLETED_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_TASK_ADDED,
    EVENT_TASK_ASSIGNED,
    EVENT_TASK_COMPLETED,
)
from .model import Task, TaskChange, WriteContext

_LOGGER = logging.getLogger(__name__)

type FamilyTasksConfigEntry = ConfigEntry[FamilyTasksCoordinator]

# TaskChange field -> Capabilities attribute
_FIELD_CAPABILITY = {
    "assignees": "assignees",
    "priority": "priority",
    "rrule": "recurrence",
    "description": "description",
}


@dataclass
class ListState:
    """Cached snapshot of one list."""

    id: str
    name: str
    tasks: dict[str, Task] = field(default_factory=dict)  # task id -> Task
    loaded: bool = False


@dataclass
class Assignee:
    """Person entity <-> tag."""

    person: str
    tag: str
    color: str | None = None


class FamilyTasksCoordinator(DataUpdateCoordinator[dict[str, ListState]]):
    """Owns the backend and the cached lists of one config entry."""

    config_entry: FamilyTasksConfigEntry

    def __init__(self, hass: HomeAssistant, entry: FamilyTasksConfigEntry) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
            always_update=True,
        )
        self.spec = get_spec(entry.data[CONF_BACKEND])
        self.backend: TaskBackend = self.spec.create(hass, entry.data)
        self.lists: dict[str, ListState] = {
            list_id: ListState(id=list_id, name=name) for list_id, name in entry.options.get(CONF_LISTS, {}).items()
        }
        self.assignees: list[Assignee] = [
            Assignee(a[ATTR_PERSON], a[ATTR_TAG], a.get(ATTR_COLOR)) for a in entry.options.get(CONF_ASSIGNEES, [])
        ]
        self.completed_days: int = entry.options.get(CONF_COMPLETED_DAYS, DEFAULT_COMPLETED_DAYS)

    # -- helpers ---------------------------------------------------------------
    @property
    def capabilities(self) -> Capabilities:
        return self.backend.capabilities

    @property
    def tag_to_person(self) -> dict[str, str]:
        return {a.tag.lower(): a.person for a in self.assignees}

    @property
    def person_to_tag(self) -> dict[str, str]:
        return {a.person: a.tag for a in self.assignees}

    @property
    def managed_tags(self) -> list[str]:
        return [a.tag for a in self.assignees]

    def context(self) -> WriteContext:
        return WriteContext(managed_tags=self.managed_tags, tz=dt_util.get_default_time_zone(), now=dt_util.utcnow())

    def list_unique_id(self, list_id: str) -> str:
        return f"{self.config_entry.entry_id}_{list_id}"

    def list_entity_id(self, list_id: str) -> str | None:
        return er.async_get(self.hass).async_get_entity_id("todo", DOMAIN, self.list_unique_id(list_id))

    def list_for_entity(self, entity_id: str) -> str | None:
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry is None or entry.config_entry_id != self.config_entry.entry_id:
            return None
        prefix = f"{self.config_entry.entry_id}_"
        return entry.unique_id.removeprefix(prefix) if entry.unique_id.startswith(prefix) else None

    def find(self, task_id: str) -> Task | None:
        for state in self.lists.values():
            if task_id in state.tasks:
                return state.tasks[task_id]
        return None

    def visible(self, task: Task, now: datetime) -> bool:
        """Open tasks, plus tasks completed within completed_days."""
        if not task.completed:
            return True
        if self.completed_days <= 0:
            return False
        # completed without a timestamp: fall back to last update
        when = task.updated if task.completed_at is None else task.completed_at
        return when is not None and now - when <= timedelta(days=self.completed_days)

    def assignees_of(self, task: Task) -> list[str]:
        mapping = self.tag_to_person
        return list(dict.fromkeys(mapping[t.lower()] for t in task.tags if t.lower() in mapping))

    def unsupported(self, change: TaskChange) -> list[str]:
        """Fields in ``change`` that this backend cannot store at all."""
        return sorted(
            name
            for name in change.touched()
            if name in _FIELD_CAPABILITY and getattr(self.capabilities, _FIELD_CAPABILITY[name]) is Support.NONE
        )

    # -- polling -----------------------------------------------------------------
    async def _async_update_data(self) -> dict[str, ListState]:
        before = {lid: dict(state.tasks) for lid, state in self.lists.items() if state.loaded}
        try:
            for state in self.lists.values():
                state.tasks = await self.backend.async_fetch(state.id)
                state.loaded = True
        except BackendAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from None
        except BackendError as err:
            raise UpdateFailed(str(err)) from None
        self._fire_events(before)
        return self.lists

    def _fire_events(self, before: dict[str, dict[str, Task]]) -> None:
        """Diff against the previous snapshot; fires for changes from any client."""
        for list_id, state in self.lists.items():
            old = before.get(list_id)
            if old is None:
                continue  # first load is the baseline
            entity_id = self.list_entity_id(list_id)
            for task_id, task in state.tasks.items():
                prev = old.get(task_id)
                if prev is not None and prev == task:
                    continue
                base = {"uid": task_id, "summary": task.summary, "list": entity_id}
                now_assigned = self.assignees_of(task)
                if prev is None:
                    self.hass.bus.async_fire(EVENT_TASK_ADDED, {**base, "assignees": now_assigned})
                    added = now_assigned
                else:
                    if task.completed and not prev.completed:
                        self.hass.bus.async_fire(
                            EVENT_TASK_COMPLETED, {**base, "assignees": now_assigned, "recurring": False}
                        )
                    added = [p for p in now_assigned if p not in self.assignees_of(prev)]
                if added:
                    self.hass.bus.async_fire(
                        EVENT_TASK_ASSIGNED,
                        {
                            **base,
                            "persons": added,
                            "tags": [self.person_to_tag[p] for p in added],
                            "assignees": now_assigned,
                        },
                    )

    # -- writes --------------------------------------------------------------------
    async def async_create(self, list_id: str, summary: str, change: TaskChange) -> str:
        if list_id not in self.lists:
            raise HomeAssistantError(f"List {list_id} is not exposed by this integration")
        task_id = await self._guard(self.backend.async_create(list_id, summary, change, self.context()))
        await self.async_refresh()
        return task_id

    async def async_update(self, task_id: str, change: TaskChange) -> bool:
        task = self.find(task_id)
        if task is None:
            await self.async_refresh()
            task = self.find(task_id)
        if task is None:
            raise HomeAssistantError(f"Task {task_id} not found")
        was_open = not task.completed
        changed = await self._guard(self.backend.async_update(task.list_id, task_id, change, self.context()))
        await self.async_refresh()
        after = self.find(task_id)
        if change.completed and was_open and after is not None and not after.completed:
            # a recurring task advanced to its next occurrence; the diff can't see that as a completion
            self.hass.bus.async_fire(
                EVENT_TASK_COMPLETED,
                {
                    "uid": task_id,
                    "summary": after.summary,
                    "list": self.list_entity_id(after.list_id),
                    "assignees": self.assignees_of(after),
                    "recurring": True,
                },
            )
        return changed

    async def async_delete(self, task_id: str) -> None:
        task = self.find(task_id)
        if task is None:
            raise HomeAssistantError(f"Task {task_id} not found")
        try:
            await self._guard(self.backend.async_delete(task.list_id, task_id))
        except HomeAssistantError as err:
            if not isinstance(err.__cause__, TaskNotFoundError):
                raise
        await self.async_refresh()

    async def _guard[T](self, awaitable: Awaitable[T]) -> T:
        """Translate backend errors into HomeAssistantError (safe messages)."""
        try:
            return await awaitable
        except ConflictError as err:
            raise HomeAssistantError(
                "The task was changed elsewhere at the same time; please reload and try again"
            ) from err
        except BackendAuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(f"The service rejected the credentials: {err}") from err
        except BackendError as err:
            raise HomeAssistantError(f"The service request failed: {err}") from err
