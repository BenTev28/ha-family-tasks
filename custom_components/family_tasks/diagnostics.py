"""Diagnostics with credentials and account details redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .coordinator import FamilyTasksConfigEntry

# redacted for every backend, on top of each backend's own keys
ALWAYS_REDACT = {"password", "url", "username", "token", "access_token", "refresh_token", "client_secret"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: FamilyTasksConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    redact = ALWAYS_REDACT | set(coordinator.spec.redact)
    return {
        "entry": {
            "title": "**REDACTED**",
            "unique_id": "**REDACTED**",
            "data": async_redact_data(dict(entry.data), redact),
        },
        "service": coordinator.spec.key,
        "capabilities": coordinator.capabilities.as_dict(),
        # list ids can contain account names (e.g. CalDAV paths): show names only
        "options": {**entry.options, "lists": sorted(entry.options.get("lists", {}).values())},
        "last_update_success": coordinator.last_update_success,
        "lists": [
            {
                "name": state.name,
                "loaded": state.loaded,
                "task_count": len(state.tasks),
                "open_count": sum(not t.completed for t in state.tasks.values()),
            }
            for state in coordinator.lists.values()
        ],
    }
