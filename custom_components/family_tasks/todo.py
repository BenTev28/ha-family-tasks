"""To-do entities: one per exposed Nextcloud task list."""

from __future__ import annotations

from datetime import date, datetime

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .coordinator import FamilyTasksConfigEntry, FamilyTasksCoordinator
from .task import Task, TaskEditor

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FamilyTasksConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(FamilyTodoList(coordinator, path) for path in coordinator.lists)


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
    """A Nextcloud task list with assignee-aware extras via family_tasks actions.

    Move is intentionally not supported: phones (Tasks.org) and the card sort
    by due date/person, and Nextcloud's X-APPLE-SORT-ORDER would not keep the
    same order across clients, so a manual HA order would be misleading.
    """

    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
        | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
        | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )

    def __init__(self, coordinator: FamilyTasksCoordinator, path: str) -> None:
        super().__init__(coordinator)
        self._path = path
        self._attr_unique_id = coordinator.list_unique_id(path)
        self._attr_name = coordinator.lists[path].name

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.lists[self._path].loaded

    @property
    def todo_items(self) -> list[TodoItem] | None:
        state = self.coordinator.lists.get(self._path)
        if state is None or not state.loaded:
            return None
        now = dt_util.now()
        tasks = [t for t in state.tasks.values() if self.coordinator.visible(t, now)]
        return [
            TodoItem(
                uid=t.uid,
                summary=t.summary,
                status=TodoItemStatus.COMPLETED if t.completed else TodoItemStatus.NEEDS_ACTION,
                due=t.due,
                description=t.description,
                completed=t.completed_at,
            )
            for t in sorted(tasks, key=sort_key)
        ]

    async def async_create_todo_item(self, item: TodoItem) -> None:
        def mutate(editor: TaskEditor) -> None:
            if item.due is not None:
                editor.set_due(item.due)
            editor.set_description(item.description)
            if item.status == TodoItemStatus.COMPLETED:
                editor.complete(dt_util.utcnow())

        await self.coordinator.async_create(self._path, item.summary or "", mutate)

    async def async_update_todo_item(self, item: TodoItem) -> None:
        assert item.uid
        await async_apply_update(
            self.coordinator,
            item.uid,
            summary=item.summary,
            due=item.due,
            set_due=True,
            description=item.description,
            set_description=True,
            completed=None if item.status is None else item.status == TodoItemStatus.COMPLETED,
        )

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        for uid in uids:
            await self.coordinator.async_delete(uid)


async def async_apply_update(
    coordinator: FamilyTasksCoordinator,
    uid: str,
    *,
    summary: str | None = None,
    due: date | datetime | None = None,
    set_due: bool = False,
    description: str | None = None,
    set_description: bool = False,
    priority: int | None = None,
    set_priority: bool = False,
    rrule: str | None = None,
    set_rrule: bool = False,
    assignee_tags: list[str] | None = None,
    add_tags: list[str] | None = None,
    remove_tags: list[str] | None = None,
    completed: bool | None = None,
) -> bool:
    """Shared by the todo entity and the family_tasks.* actions."""
    advanced = False

    def mutate(editor: TaskEditor) -> None:
        nonlocal advanced
        if summary is not None:
            editor.set_summary(summary)
        if set_description:
            editor.set_description(description)
        if set_priority:
            editor.set_priority(priority)
        if set_rrule:
            editor.set_rrule(rrule)
        if set_due:
            editor.set_due(due)
        if assignee_tags is not None or add_tags or remove_tags:
            current = Task.from_ical("", None, editor.doc.serialize(), editor.tz)
            managed = coordinator.managed_tags
            managed_lc = {m.lower(): m for m in managed}
            wanted = (
                list(assignee_tags)
                if assignee_tags is not None
                else [managed_lc[c.lower()] for c in current.categories if c.lower() in managed_lc]
            )
            for tag in add_tags or []:
                if tag.lower() not in {w.lower() for w in wanted}:
                    wanted.append(tag)
            drop = {t.lower() for t in remove_tags or []}
            wanted = [w for w in wanted if w.lower() not in drop]
            editor.set_assignee_tags(wanted, managed)
        if completed is not None:
            is_done = Task.from_ical("", None, editor.doc.serialize(), editor.tz).completed
            if completed and not is_done:
                advanced = editor.complete(dt_util.utcnow())
            elif not completed and is_done:
                editor.reopen()

    changed = await coordinator.async_mutate(uid, mutate)
    if advanced:
        found = coordinator.find(uid)
        if found:
            coordinator.fire_recurring_completed(found[1])
    return changed
