"""Per-person open task counts."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util, slugify

from .coordinator import Assignee, FamilyTasksConfigEntry, FamilyTasksCoordinator
from .task import Task

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FamilyTasksConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(OpenTasksSensor(coordinator, a) for a in coordinator.assignees)


def open_tasks_for(coordinator: FamilyTasksCoordinator, person: str) -> list[Task]:
    tag = coordinator.person_to_tag[person].lower()
    result = [
        task
        for state in coordinator.lists.values()
        for task in state.tasks.values()
        if not task.completed and tag in {c.lower() for c in task.categories}
    ]
    return sorted(result, key=lambda t: (t.due is None, str(t.due or ""), t.summary.lower()))


class PersonEntity(CoordinatorEntity[FamilyTasksCoordinator]):
    """Base for per-person entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FamilyTasksCoordinator, assignee: Assignee, key: str) -> None:
        super().__init__(coordinator)
        self._person = assignee.person
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{assignee.person}_{key}"
        self._attr_translation_key = key
        self._attr_translation_placeholders = {"person": assignee.tag}
        # entity_id: sensor.<tag>_open_family_tasks
        self.entity_id = f"{self._platform_domain}.{slugify(assignee.tag)}_{key}"

    _platform_domain = "sensor"


class OpenTasksSensor(PersonEntity, SensorEntity):
    """Number of open family tasks assigned to a person."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "tasks"

    def __init__(self, coordinator: FamilyTasksCoordinator, assignee: Assignee) -> None:
        super().__init__(coordinator, assignee, "open_family_tasks")

    @property
    def native_value(self) -> int:
        return len(open_tasks_for(self.coordinator, self._person))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = dt_util.now()
        tasks = open_tasks_for(self.coordinator, self._person)
        return {
            "person": self._person,
            "tasks": [t.summary for t in tasks],
            "overdue": [t.summary for t in tasks if t.is_overdue(now)],
        }
