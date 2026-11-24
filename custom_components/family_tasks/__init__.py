"""Family Tasks: Nextcloud (CalDAV) family task list with assignees as CATEGORIES."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .const import CARD_FILENAME, DOMAIN, FRONTEND_URL, SIGNAL_UPDATED, VERSION
from .coordinator import FamilyTasksConfigEntry, FamilyTasksCoordinator
from .services import async_setup_services
from .websocket_api import async_setup_websocket

PLATFORMS = [Platform.TODO, Platform.SENSOR, Platform.BINARY_SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register actions, websocket commands and the Lovelace card."""
    async_setup_services(hass)
    async_setup_websocket(hass)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(FRONTEND_URL, str(Path(__file__).parent / "frontend"), True)]
    )
    # Loaded as a module on every frontend page: works in storage and YAML
    # dashboard modes without touching the Lovelace resource list.
    if "frontend" in hass.config.components:
        add_extra_js_url(hass, f"{FRONTEND_URL}/{CARD_FILENAME}?v={VERSION}")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: FamilyTasksConfigEntry) -> bool:
    coordinator = FamilyTasksCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    # one dispatcher signal for websocket subscribers, so card subscriptions
    # survive config entry reloads
    entry.async_on_unload(coordinator.async_add_listener(lambda: async_dispatcher_send(hass, SIGNAL_UPDATED)))
    async_dispatcher_send(hass, SIGNAL_UPDATED)
    return True


async def _async_reload(hass: HomeAssistant, entry: FamilyTasksConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: FamilyTasksConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    async_dispatcher_send(hass, SIGNAL_UPDATED)
    return unloaded
