"""Diagnostics with credentials and server URL redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .coordinator import FamilyTasksConfigEntry

TO_REDACT = {CONF_PASSWORD, CONF_URL, CONF_USERNAME, "unique_id", "title"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: FamilyTasksConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    return {
        "entry": async_redact_data(
            {"title": entry.title, "unique_id": entry.unique_id, "data": dict(entry.data)}, TO_REDACT
        ),
        "options": dict(entry.options),
        "last_update_success": coordinator.last_update_success,
        "lists": {
            path: {
                "name": state.name,
                "loaded": state.loaded,
                "task_count": len(state.tasks),
                "open_count": sum(not t.completed for t in state.tasks.values()),
                "uses_sync_token": state.sync_token is not None,
                "has_ctag": state.ctag is not None,
            }
            for path, state in coordinator.lists.items()
        },
    }
