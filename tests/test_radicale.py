"""End-to-end tests: Home Assistant + family_tasks against a real Radicale server.

Run via ./scripts_test.sh (starts Radicale) or set RADICALE_URL yourself.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import date
import os
from pathlib import Path
import uuid

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.family_tasks.backends.base import BackendConnectionError
from custom_components.family_tasks.backends.caldav.client import CalDavClient, PreconditionFailed
from custom_components.family_tasks.const import (
    CONF_ASSIGNEES,
    CONF_COMPLETED_DAYS,
    CONF_LISTS,
    CONF_SCAN_INTERVAL,
    DOMAIN,
    EVENT_TASK_ADDED,
    EVENT_TASK_ASSIGNED,
    EVENT_TASK_COMPLETED,
)
from custom_components.family_tasks.coordinator import FamilyTasksCoordinator
from custom_components.family_tasks.diagnostics import async_get_config_entry_diagnostics

RADICALE_URL = os.environ.get("RADICALE_URL")
pytestmark = [
    pytest.mark.radicale,
    pytest.mark.skipif(not RADICALE_URL, reason="RADICALE_URL not set"),
]

SAMPLE = (Path(__file__).parent / "fixtures" / "tasks_org_sample.ics").read_text(newline="")
PASSWORD = "s3cret-app-password-DO-NOT-LOG"
LIST = "todo.family_to_do"


@pytest.fixture(autouse=True)
def _allow_localhost(socket_enabled):
    """Radicale runs on 127.0.0.1; pytest-homeassistant blocks sockets by default."""


async def _req(session: aiohttp.ClientSession, method: str, url: str, user: str, body: str = "", **headers: str):
    token = base64.b64encode(f"{user}:{PASSWORD}".encode()).decode()
    async with session.request(
        method, url, data=body.encode(), headers={**headers, "Authorization": f"Basic {token}"}
    ) as resp:
        return resp.status, await resp.text(), resp.headers.get("ETag")


@pytest.fixture
async def account() -> AsyncIterator[dict[str, str]]:
    """A fresh Radicale user with a VTODO list and a VEVENT-only calendar."""
    user = f"u{uuid.uuid4().hex[:8]}"
    base = RADICALE_URL.rstrip("/")
    async with aiohttp.ClientSession() as session:
        await _req(session, "PROPFIND", f"{base}/{user}/", user, Depth="0")
        for slug, name, comp in (("family-to-do", "Family To-Do", "VTODO"), ("events", "Events", "VEVENT")):
            body = (
                '<?xml version="1.0"?><c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                f"<d:set><d:prop><d:displayname>{name}</d:displayname>"
                f'<c:supported-calendar-component-set><c:comp name="{comp}"/></c:supported-calendar-component-set>'
                "</d:prop></d:set></c:mkcalendar>"
            )
            status, text, _ = await _req(session, "MKCALENDAR", f"{base}/{user}/{slug}/", user, body)
            assert status in (201, 200), text
    yield {"user": user, "base": base, "list": f"/{user}/family-to-do/"}


async def _remote_put(account: dict[str, str], name: str, ics: str, etag: str | None = None) -> str | None:
    """Write like a phone would (bypassing HA)."""
    headers = {"Content-Type": "text/calendar"}
    if etag:
        headers["If-Match"] = etag
    async with aiohttp.ClientSession() as session:
        status, text, new_etag = await _req(
            session, "PUT", f"{account['base']}{account['list']}{name}", account["user"], ics, **headers
        )
    assert status in (200, 201, 204), text
    return new_etag


async def _remote_get(account: dict[str, str], name: str) -> tuple[str, str | None]:
    async with aiohttp.ClientSession() as session:
        status, text, etag = await _req(session, "GET", f"{account['base']}{account['list']}{name}", account["user"])
    assert status == 200
    return text, etag


async def _remote_list(account: dict[str, str]) -> list[str]:
    async with aiohttp.ClientSession() as session:
        client = CalDavClient(session, account["base"], account["user"], PASSWORD)
        return [r.data or "" for r in await client.fetch_all(account["list"])]


@pytest.fixture
async def entry(hass: HomeAssistant, account: dict[str, str]) -> MockConfigEntry:
    await hass.config.async_set_time_zone("America/Los_Angeles")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test",
        unique_id=account["user"],
        data={
            "backend": "caldav",
            CONF_URL: account["base"],
            CONF_USERNAME: account["user"],
            CONF_PASSWORD: PASSWORD,
            CONF_VERIFY_SSL: True,
        },
        options={
            CONF_LISTS: {account["list"]: "Family To-Do"},
            CONF_ASSIGNEES: [
                {"person": "person.hannah_tevelow", "tag": "Hannah", "color": "#1e88e5"},
                {"person": "person.rachel_tevelow", "tag": "Rachel"},
                {"person": "person.owen", "tag": "Owen"},
                {"person": "person.micah", "tag": "Micah"},
            ],
            CONF_SCAN_INTERVAL: 60,
            CONF_COMPLETED_DAYS: 7,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _coord(entry: MockConfigEntry) -> FamilyTasksCoordinator:
    return entry.runtime_data


async def _get_tasks(hass: HomeAssistant, **data) -> list[dict]:
    resp = await hass.services.async_call(DOMAIN, "get_tasks", data, blocking=True, return_response=True)
    return resp["tasks"]


# ---------------------------------------------------------------------------


async def test_discovery_only_vtodo(account: dict[str, str]) -> None:
    async with aiohttp.ClientSession() as session:
        client = CalDavClient(session, account["base"], account["user"], PASSWORD)
        collections = await client.discover()
    assert [(c.path, c.name) for c in collections] == [(account["list"], "Family To-Do")]


async def test_add_task_with_two_assignees(hass: HomeAssistant, entry, account) -> None:
    added = async_capture_events(hass, EVENT_TASK_ADDED)
    resp = await hass.services.async_call(
        DOMAIN,
        "add_task",
        {
            "list": LIST,
            "summary": "Feed the cat",
            "assignees": ["person.owen", "person.micah"],
            "due_date": "2026-11-30",
            "priority": "high",
        },
        blocking=True,
        return_response=True,
    )
    raws = await _remote_list(account)
    assert len(raws) == 1  # one VTODO, not one per person
    assert "CATEGORIES:Owen,Micah" in raws[0]
    assert "DUE;VALUE=DATE:20261130" in raws[0]
    assert "PRIORITY:1" in raws[0]
    assert len(added) == 1 and added[0].data["uid"] == resp["uid"]
    assert added[0].data["assignees"] == ["person.owen", "person.micah"]

    state = hass.states.get(LIST)
    assert state is not None and state.state == "1"
    assert hass.states.get("sensor.owen_open_family_tasks").state == "1"
    assert hass.states.get("sensor.hannah_open_family_tasks").state == "0"

    tasks = await _get_tasks(hass, assignee="person.micah")
    assert [t["summary"] for t in tasks] == ["Feed the cat"]
    assert tasks[0]["priority"] == "high"


async def test_unknown_person_rejected(hass: HomeAssistant, entry) -> None:
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "add_task",
            {"list": LIST, "summary": "x", "assignees": ["person.nobody"]},
            blocking=True,
        )


async def test_phone_tag_shows_up_and_fires_assigned(hass: HomeAssistant, entry, account) -> None:
    await _remote_put(account, "phone.ics", SAMPLE)
    await _coord(entry).async_refresh()
    assigned = async_capture_events(hass, EVENT_TASK_ASSIGNED)

    raw, etag = await _remote_get(account, "phone.ics")
    await _remote_put(
        account, "phone.ics", raw.replace("CATEGORIES:Groceries,owen", "CATEGORIES:Groceries,owen,Rachel"), etag
    )
    await _coord(entry).async_refresh()
    await hass.async_block_till_done()

    assert len(assigned) == 1
    assert assigned[0].data["persons"] == ["person.rachel_tevelow"]
    tasks = await _get_tasks(hass)
    assert set(tasks[0]["assignees"]) == {"person.owen", "person.rachel_tevelow"}
    assert tasks[0]["other_tags"] == ["Groceries"]
    assert hass.states.get("sensor.rachel_open_family_tasks").state == "1"


async def test_reassign_keeps_hand_added_tag_and_tasks_org_props(hass: HomeAssistant, entry, account) -> None:
    await _remote_put(account, "phone.ics", SAMPLE.replace("CATEGORIES:Groceries,owen", "CATEGORIES:Micah,Groceries"))
    await _coord(entry).async_refresh()
    uid = "7340213419562178372"
    await hass.services.async_call(
        DOMAIN, "update_task", {"uid": uid, "assignees": ["person.owen"], "summary": "Bins"}, blocking=True
    )
    raw, _ = await _remote_get(account, "phone.ics")
    assert "CATEGORIES:Groceries,Owen" in raw
    for needle in (
        "RELATED-TO:4093487710209877712",
        "X-TASKS-ORG-CUSTOM:keep-me",
        "X-APPLE-SORT-ORDER:722199000",
        "TRIGGER;RELATED=END:PT0S",
        "RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=WE",
    ):
        assert needle in raw
    assert raw.count("BEGIN:VALARM") == 2
    assert "SEQUENCE:1" in raw


async def test_conflicting_edits_merge_not_overwrite(hass: HomeAssistant, entry, account) -> None:
    await _remote_put(account, "phone.ics", SAMPLE)
    await _coord(entry).async_refresh()
    # phone edits the title; HA has not polled yet so its ETag is stale
    raw, etag = await _remote_get(account, "phone.ics")
    await _remote_put(
        account, "phone.ics", raw.replace("SUMMARY:Take the recycling", "SUMMARY:PHONE EDIT Take the recycling"), etag
    )

    await hass.services.async_call(
        DOMAIN, "update_task", {"uid": "7340213419562178372", "add_assignees": ["person.hannah_tevelow"]}, blocking=True
    )
    raw, _ = await _remote_get(account, "phone.ics")
    assert "SUMMARY:PHONE EDIT" in raw  # phone's change survived
    assert "CATEGORIES:Groceries,Owen,Hannah" in raw  # HA change applied (configured case)


async def test_persistent_conflict_raises(hass: HomeAssistant, entry, account, monkeypatch) -> None:
    await _remote_put(account, "phone.ics", SAMPLE)
    await _coord(entry).async_refresh()

    async def always_412(*args, **kwargs):
        raise PreconditionFailed("changed")

    monkeypatch.setattr(_coord(entry).backend.client, "put", always_412)
    with pytest.raises(HomeAssistantError, match="changed elsewhere"):
        await hass.services.async_call(
            DOMAIN, "update_task", {"uid": "7340213419562178372", "summary": "x"}, blocking=True
        )


async def test_complete_recurring_advances(hass: HomeAssistant, entry, account) -> None:
    await _remote_put(account, "phone.ics", SAMPLE)
    await _coord(entry).async_refresh()
    completed = async_capture_events(hass, EVENT_TASK_COMPLETED)
    await hass.services.async_call(DOMAIN, "complete_task", {"uid": "7340213419562178372"}, blocking=True)
    await hass.async_block_till_done()
    raw, _ = await _remote_get(account, "phone.ics")
    assert "DUE;TZID=America/Los_Angeles:20261202T170000" in raw
    assert "COUNT=2" in raw
    assert "COMPLETED:" not in raw
    assert len(completed) == 1 and completed[0].data["recurring"] is True
    tasks = await _get_tasks(hass)
    assert tasks[0]["status"] == "needs_action"


async def test_complete_and_reopen_via_todo_entity(hass: HomeAssistant, entry, account) -> None:
    completed = async_capture_events(hass, EVENT_TASK_COMPLETED)
    await hass.services.async_call(
        "todo",
        "add_item",
        {"item": "Plain task", "due_date": date(2026, 11, 20)},
        target={"entity_id": LIST},
        blocking=True,
    )
    assert hass.states.get(LIST).state == "1"
    await hass.services.async_call(
        "todo", "update_item", {"item": "Plain task", "status": "completed"}, target={"entity_id": LIST}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(LIST).state == "0"
    assert len(completed) == 1 and completed[0].data["recurring"] is False
    raws = await _remote_list(account)
    assert "STATUS:COMPLETED" in raws[0]
    await hass.services.async_call(
        "todo",
        "update_item",
        {"item": "Plain task", "status": "needs_action"},
        target={"entity_id": LIST},
        blocking=True,
    )
    assert hass.states.get(LIST).state == "1"
    assert hass.states.get("binary_sensor.owen_has_overdue_family_tasks").state == "off"


async def test_overdue_sensor(hass: HomeAssistant, entry) -> None:
    await hass.services.async_call(
        DOMAIN,
        "add_task",
        {"list": LIST, "summary": "Late", "assignees": ["person.micah"], "due_date": "2020-01-01"},
        blocking=True,
    )
    state = hass.states.get("binary_sensor.micah_has_overdue_family_tasks")
    assert state.state == "on"
    assert state.attributes["tasks"] == ["Late"]
    overdue = await _get_tasks(hass, due="overdue")
    assert [t["summary"] for t in overdue] == ["Late"]


async def test_remote_delete_detected_via_sync(hass: HomeAssistant, entry, account) -> None:
    await _remote_put(account, "phone.ics", SAMPLE)
    await _coord(entry).async_refresh()
    state = _coord(entry).backend._lists[account["list"]]
    assert state.sync_token  # Radicale supports sync-collection
    async with aiohttp.ClientSession() as session:
        await _req(session, "DELETE", f"{account['base']}{account['list']}phone.ics", account["user"])
    await _coord(entry).async_refresh()
    assert await _get_tasks(hass) == []


async def test_refresh_action_fetches_now(hass: HomeAssistant, entry, account) -> None:
    await _remote_put(account, "phone.ics", SAMPLE)
    assert await _get_tasks(hass) == []  # not polled yet
    await hass.services.async_call(DOMAIN, "refresh", {"list": LIST}, blocking=True)
    assert [t["uid"] for t in await _get_tasks(hass)] == ["7340213419562178372"]
    async with aiohttp.ClientSession() as session:
        await _req(session, "DELETE", f"{account['base']}{account['list']}phone.ics", account["user"])
    await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)  # every account
    assert await _get_tasks(hass) == []


async def test_refresh_unknown_list_rejected(hass: HomeAssistant, entry) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "refresh", {"list": "todo.not_ours"}, blocking=True)


async def test_refresh_failure_raises_without_details(hass: HomeAssistant, entry, monkeypatch) -> None:
    async def unreachable(*args, **kwargs):
        raise BackendConnectionError("https://user:pw@dav.example/ unreachable")

    monkeypatch.setattr(_coord(entry).backend, "async_fetch", unreachable)
    with pytest.raises(HomeAssistantError, match="Couldn't reach the task service") as err:
        await hass.services.async_call(DOMAIN, "refresh", {"list": LIST}, blocking=True)
    assert "dav.example" not in str(err.value)


async def test_delete_task(hass: HomeAssistant, entry, account) -> None:
    resp = await hass.services.async_call(
        DOMAIN, "add_task", {"list": LIST, "summary": "temp"}, blocking=True, return_response=True
    )
    await hass.services.async_call(DOMAIN, "delete_task", {"uid": resp["uid"]}, blocking=True)
    assert await _remote_list(account) == []


async def test_no_credentials_in_logs_or_diagnostics(hass: HomeAssistant, entry, account, caplog) -> None:
    caplog.set_level("DEBUG", logger="custom_components.family_tasks")
    await hass.services.async_call(DOMAIN, "add_task", {"list": LIST, "summary": "x"}, blocking=True)
    await _coord(entry).async_refresh()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert PASSWORD not in caplog.text
    assert account["base"] not in caplog.text  # no server URL, not even at DEBUG
    text = str(diag)
    assert PASSWORD not in text
    assert account["base"] not in text


async def test_ws_config_reports_capabilities(hass: HomeAssistant, entry, hass_ws_client) -> None:
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "family_tasks/config"})
    msg = await client.receive_json()
    assert msg["success"]
    lst = msg["result"]["lists"][0]
    assert lst["entity_id"] == LIST
    assert lst["service"] == "caldav"
    assert lst["capabilities"]["assignees"] == "native"
    assert lst["capabilities"]["due_time"] is True


async def test_unsupported_field_is_rejected_not_dropped(hass: HomeAssistant, entry, monkeypatch) -> None:
    """Same action, different service: a field the service can't store raises a clear error."""
    from homeassistant.exceptions import ServiceValidationError

    from custom_components.family_tasks.backends.base import Capabilities, Support

    backend = _coord(entry).backend
    monkeypatch.setattr(
        backend,
        "capabilities",
        Capabilities(assignees=Support.NATIVE, priority=Support.NONE, recurrence=Support.NONE, due_time=False),
    )
    for extra in ({"priority": "high"}, {"rrule": "FREQ=DAILY"}, {"due_datetime": "2026-12-01 10:00:00"}):
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(DOMAIN, "add_task", {"list": LIST, "summary": "x", **extra}, blocking=True)
    # supported fields still work
    await hass.services.async_call(
        DOMAIN, "add_task", {"list": LIST, "summary": "ok", "assignees": ["person.owen"]}, blocking=True
    )
