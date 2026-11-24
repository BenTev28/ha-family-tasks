"""Per-person overdue indicator."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import Assignee, FamilyTasksConfigEntry, FamilyTasksCoordinator
from .sensor import PersonEntity, open_tasks_for

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FamilyTasksConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(OverdueBinarySensor(coordinator, a) for a in coordinator.assignees)


class OverdueBinarySensor(PersonEntity, BinarySensorEntity):
    """On when the person has an open, overdue family task."""

    _platform_domain = "binary_sensor"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: FamilyTasksCoordinator, assignee: Assignee) -> None:
        super().__init__(coordinator, assignee, "has_overdue_family_tasks")

    def _overdue(self) -> list[str]:
        now = dt_util.now()
        return [t.summary for t in open_tasks_for(self.coordinator, self._person) if t.is_overdue(now)]

    @property
    def is_on(self) -> bool:
        return bool(self._overdue())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"person": self._person, "tasks": self._overdue()}
