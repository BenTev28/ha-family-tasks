"""WebSocket commands used by family-tasks-card."""

from __future__ import annotations

from typing import Any

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
import voluptuous as vol

from .const import DOMAIN, SIGNAL_UPDATED
from .services import query_tasks


@callback
def async_setup_websocket(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_config)
    websocket_api.async_register_command(hass, ws_subscribe)


def _coordinators(hass: HomeAssistant) -> list[Any]:
    return [e.runtime_data for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED]


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/config"})
@callback
def ws_config(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    people: list[dict[str, Any]] = []
    lists: list[dict[str, Any]] = []
    seen: set[str] = set()
    completed_days = 0
    for coordinator in _coordinators(hass):
        completed_days = max(completed_days, coordinator.completed_days)
        for a in coordinator.assignees:
            if a.person in seen:
                continue
            seen.add(a.person)
            state = hass.states.get(a.person)
            people.append({"person": a.person, "tag": a.tag, "name": state.name if state else a.tag, "color": a.color})
        for path, list_state in coordinator.lists.items():
            entity_id = coordinator.list_entity_id(path)
            if entity_id:
                lists.append({"entity_id": entity_id, "name": list_state.name})
    connection.send_result(msg["id"], {"people": people, "lists": lists, "completed_days": completed_days})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subscribe", vol.Optional("entity_id"): str})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    entity_id = msg.get("entity_id")

    @callback
    def push() -> None:
        connection.send_message(
            websocket_api.event_message(
                msg["id"],
                {"tasks": query_tasks(hass, list=entity_id, status="all", visible_only=True)},
            )
        )

    unsub = async_dispatcher_connect(hass, SIGNAL_UPDATED, push)

    connection.subscriptions[msg["id"]] = unsub
    connection.send_result(msg["id"])
    push()
