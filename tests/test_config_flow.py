"""Config / options / reconfigure flow tests (CalDAV mocked)."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous_serialize

from custom_components.family_tasks.backends.caldav import InvalidUrl, validate_url
from custom_components.family_tasks.backends.caldav.client import (
    CalDavAuthError,
    CalDavConnectionError,
    Collection,
)
from custom_components.family_tasks.const import CONF_ASSIGNEES, CONF_LISTS, DOMAIN

COLLECTIONS = [
    Collection("/remote.php/dav/calendars/household/family-to-do/", "Family To-Do", None, "1", "t"),
    Collection("/remote.php/dav/calendars/household/tasks/", "Tasks", None, "1", "t"),
]
USER_INPUT = {
    "url": "https://cloud.example.com/remote.php/dav",
    "username": "household",
    "password": "app-pass",
    "verify_ssl": True,
}
DISCOVER = "custom_components.family_tasks.backends.caldav.client.CalDavClient.discover"


@pytest.mark.parametrize(
    "url",
    [
        "http://cloud.example.com/remote.php/dav",
        "https://user:pass@cloud.example.com/remote.php/dav",
        "https://pass@cloud.example.com",
        "cloud.example.com",
        "https://cloud.example.com/dav?x=1",
    ],
)
def test_validate_url_rejects(url: str) -> None:
    with pytest.raises(InvalidUrl):
        validate_url(url)


def test_validate_url_accepts() -> None:
    assert validate_url(" https://cloud.example.com/remote.php/dav/ ") == "https://cloud.example.com/remote.php/dav"


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def test_full_flow_defaults_to_family_list(hass: HomeAssistant) -> None:
    hass.states.async_set("person.owen", "home", {"friendly_name": "Owen"})
    hass.states.async_set("person.hannah_tevelow", "home", {"friendly_name": "Hannah Tevelow"})
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    with patch(DISCOVER, return_value=COLLECTIONS):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["step_id"] == "lists"
    schema_defaults = {str(k): k.default() for k in result["data_schema"].schema if hasattr(k, "default")}
    assert schema_defaults[CONF_LISTS] == [COLLECTIONS[0].path]
    with patch("custom_components.family_tasks.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_LISTS: [COLLECTIONS[0].path]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_LISTS] == {COLLECTIONS[0].path: "Family To-Do"}
    tags = {a["person"]: a["tag"] for a in result["options"][CONF_ASSIGNEES]}
    assert tags == {"person.hannah_tevelow": "Hannah", "person.owen": "Owen"}


@pytest.mark.parametrize(
    ("side_effect", "error"),
    [(CalDavAuthError("x"), "invalid_auth"), (CalDavConnectionError("x"), "cannot_connect")],
)
async def test_errors(hass: HomeAssistant, side_effect: Exception, error: str) -> None:
    result = await _start(hass)
    with patch(DISCOVER, side_effect=side_effect):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": error}


async def test_invalid_url_never_echoes_password(hass: HomeAssistant, caplog) -> None:
    result = await _start(hass)
    bad = {**USER_INPUT, "url": "https://household:app-pass@cloud.example.com"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], bad)
    assert result["errors"] == {"url": "invalid_url"}
    assert "app-pass" not in caplog.text


async def test_reconfigure_keeps_entry_and_password(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="caldav:household@cloud.example.com",
        data={**USER_INPUT, "backend": "caldav"},
        options={CONF_LISTS: {COLLECTIONS[0].path: "Family To-Do"}, CONF_ASSIGNEES: []},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    with (
        patch(DISCOVER, return_value=COLLECTIONS),
        patch("custom_components.family_tasks.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, "password": "new-pass"}
        )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["password"] == "new-pass"


async def test_options_flow(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="caldav:household@cloud.example.com",
        data={**USER_INPUT, "backend": "caldav"},
        options={
            CONF_LISTS: {COLLECTIONS[0].path: "Family To-Do"},
            CONF_ASSIGNEES: [{"person": "person.owen", "tag": "Owen", "color": "#43a047"}],
        },
    )
    entry.add_to_hass(hass)
    with patch(DISCOVER, return_value=COLLECTIONS):
        result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    # the form must serialise for the frontend (ObjectSelector etc.)
    voluptuous_serialize.convert(result["data_schema"], custom_serializer=cv.custom_serializer)

    dup = {
        CONF_LISTS: [COLLECTIONS[0].path],
        CONF_ASSIGNEES: [{"person": "person.owen", "tag": "Owen"}, {"person": "person.micah", "tag": "owen"}],
        "scan_interval": 60,
        "completed_days": 7,
    }
    with patch(DISCOVER, return_value=COLLECTIONS):
        result = await hass.config_entries.options.async_configure(result["flow_id"], dup)
    assert result["errors"] == {CONF_ASSIGNEES: "duplicate_assignee"}

    ok = {**dup, CONF_ASSIGNEES: [{"person": "person.owen", "tag": "Owen"}, {"person": "person.micah", "tag": "Micah"}]}
    with (
        patch(DISCOVER, return_value=COLLECTIONS),
        patch("custom_components.family_tasks.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.options.async_configure(result["flow_id"], ok)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ASSIGNEES][1] == {"person": "person.micah", "tag": "Micah"}
    assert entry.options["scan_interval"] == 60
