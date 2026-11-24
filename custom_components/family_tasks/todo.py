"""To-do entities: one per exposed list, whatever the service."""

from __future__ import annotations

from datetime import date, datetime

from homeassistant.components.todo import TodoItem, TodoItemStatus, TodoListEntity, TodoListEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .backends.base import Support
from .coordinator import FamilyTasksConfigEntry, FamilyTasksCoordinator
from .model import UNSET, Task, TaskChange

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FamilyTasksConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(FamilyTodoList(coordinator, list_id) for list_id in coordinator.lists)


def sort_key(task: Task) -> tuple:
    """Open first, then by due (undated last), then summary."""
    due = task.due
    if isinstance(due, datetime):
        key = (0, due.timestamp())
    elif isinstance(due, date):
        key = (0, datetime.combine(due, datetime.max.time()).timestamp())
    else:
        key = (1, 0.0)
    return (task.completed, *key, task.summary.lower())


class FamilyTodoList(CoordinatorEntity[FamilyTasksCoordinator], TodoListEntity):
    """A family task list. Assignees and other extras go through family_tasks.* actions.

    Move is intentionally not supported: the card and phone apps sort by due
    date or person, and services keep manual order inconsistently (or not at
    all), so a manual HA order would be misleading.
    """

    def __init__(self, coordinator: FamilyTasksCoordinator, list_id: str) -> None:
        super().__init__(coordinator)
        self._list_id = list_id
        self._attr_unique_id = coordinator.list_unique_id(list_id)
        self._attr_name = coordinator.lists[list_id].name
        caps = coordinator.capabilities
        features = (
            TodoListEntityFeature.CREATE_TODO_ITEM
            | TodoListEntityFeature.UPDATE_TODO_ITEM
            | TodoListEntityFeature.DELETE_TODO_ITEM
            | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
        )
        if caps.due_time:
            features |= TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
        if caps.description is not Support.NONE:
            features |= TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
        self._attr_supported_features = features

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.lists[self._list_id].loaded

    @property
    def todo_items(self) -> list[TodoItem] | None:
        state = self.coordinator.lists.get(self._list_id)
        if state is None or not state.loaded:
            return None
        now = dt_util.now()
        tasks = [t for t in state.tasks.values() if self.coordinator.visible(t, now)]
        return [
            TodoItem(
                uid=t.id,
                summary=t.summary,
                status=TodoItemStatus.COMPLETED if t.completed else TodoItemStatus.NEEDS_ACTION,
                due=t.due,
                description=t.description,
                completed=t.completed_at,
            )
            for t in sorted(tasks, key=sort_key)
        ]

    async def async_create_todo_item(self, item: TodoItem) -> None:
        change = TaskChange(
            due=item.due if item.due is not None else UNSET,
            description=item.description if item.description else UNSET,
            completed=True if item.status == TodoItemStatus.COMPLETED else None,
        )
        await self.coordinator.async_create(self._list_id, item.summary or "", change)

    async def async_update_todo_item(self, item: TodoItem) -> None:
        assert item.uid
        # HA passes the full merged item; unchanged fields are no-ops in the backends
        change = TaskChange(
            summary=item.summary if item.summary is not None else UNSET,
            due=item.due,
            description=item.description if self.coordinator.capabilities.description is not Support.NONE else UNSET,
            completed=None if item.status is None else item.status == TodoItemStatus.COMPLETED,
        )
        await self.coordinator.async_update(item.uid, change)

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        for uid in uids:
            await self.coordinator.async_delete(uid)
