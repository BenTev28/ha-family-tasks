"""Coordinator: polls CalDAV collections and performs conflict-safe writes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
import logging
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .client import (
    CalDavAuthError,
    CalDavClient,
    CalDavError,
    NotFound,
    PreconditionFailed,
    SyncTokenInvalid,
)
from .const import (
    ATTR_COLOR,
    ATTR_PERSON,
    ATTR_TAG,
    CONF_ASSIGNEES,
    CONF_COMPLETED_DAYS,
    CONF_LISTS,
    CONF_SCAN_INTERVAL,
    CONF_VERIFY_SSL,
    DEFAULT_COMPLETED_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_TASK_ADDED,
    EVENT_TASK_ASSIGNED,
    EVENT_TASK_COMPLETED,
    PRODID,
)
from .ics import ICalDocument
from .task import Task, TaskEditor, new_task_ical

_LOGGER = logging.getLogger(__name__)

type FamilyTasksConfigEntry = ConfigEntry[FamilyTasksCoordinator]
type Mutation = Callable[[TaskEditor], None]


@dataclass
class ListState:
    """Cached state of one collection."""

    path: str
    name: str
    tasks: dict[str, Task] = field(default_factory=dict)  # href -> Task
    ctag: str | None = None
    sync_token: str | None = None
    loaded: bool = False


@dataclass
class Assignee:
    """Person entity <-> CATEGORIES tag."""

    person: str
    tag: str
    color: str | None = None


class FamilyTasksCoordinator(DataUpdateCoordinator[dict[str, ListState]]):
    """Owns the CalDAV client and the cached task lists of one config entry."""

    config_entry: FamilyTasksConfigEntry

    def __init__(self, hass: HomeAssistant, entry: FamilyTasksConfigEntry) -> None:
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
            always_update=True,
        )
        self.client = CalDavClient(
            async_get_clientsession(hass, verify_ssl=entry.data.get(CONF_VERIFY_SSL, True)),
            entry.data[CONF_URL],
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
            entry.data.get(CONF_VERIFY_SSL, True),
        )
        self.lists: dict[str, ListState] = {
            path: ListState(path=path, name=name) for path, name in entry.options.get(CONF_LISTS, {}).items()
        }
        self.assignees: list[Assignee] = [
            Assignee(a[ATTR_PERSON], a[ATTR_TAG], a.get(ATTR_COLOR)) for a in entry.options.get(CONF_ASSIGNEES, [])
        ]
        self.completed_days: int = entry.options.get(CONF_COMPLETED_DAYS, DEFAULT_COMPLETED_DAYS)
        self._version = 0  # bumps on every data change (for websocket subscribers)

    # -- helpers -------------------------------------------------------------
    @property
    def tz(self) -> tzinfo:
        return ZoneInfo(self.hass.config.time_zone)

    @property
    def tag_to_person(self) -> dict[str, str]:
        return {a.tag.lower(): a.person for a in self.assignees}

    @property
    def person_to_tag(self) -> dict[str, str]:
        return {a.person: a.tag for a in self.assignees}

    @property
    def managed_tags(self) -> list[str]:
        return [a.tag for a in self.assignees]

    def list_unique_id(self, path: str) -> str:
        return f"{self.config_entry.entry_id}_{path}"

    def list_entity_id(self, path: str) -> str | None:
        return er.async_get(self.hass).async_get_entity_id("todo", DOMAIN, self.list_unique_id(path))

    def path_for_entity(self, entity_id: str) -> str | None:
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry is None or entry.config_entry_id != self.config_entry.entry_id:
            return None
        prefix = f"{self.config_entry.entry_id}_"
        return entry.unique_id.removeprefix(prefix) if entry.unique_id.startswith(prefix) else None

    def find(self, uid: str) -> tuple[ListState, Task] | None:
        for state in self.lists.values():
            for task in state.tasks.values():
                if task.uid == uid:
                    return state, task
        return None

    def visible(self, task: Task, now: datetime) -> bool:
        """Open tasks, plus tasks completed within completed_days."""
        if not task.completed:
            return True
        if self.completed_days <= 0:
            return False
        # completed without a timestamp: fall back to LAST-MODIFIED
        when = task.last_modified if task.completed_at is None else task.completed_at
        return when is not None and now - when <= timedelta(days=self.completed_days)

    def assignees_of(self, task: Task) -> list[str]:
        mapping = self.tag_to_person
        return list(dict.fromkeys(mapping[c.lower()] for c in task.categories if c.lower() in mapping))

    # -- polling ---------------------------------------------------------------
    async def _async_update_data(self) -> dict[str, ListState]:
        before = {path: {t.uid: t for t in state.tasks.values()} for path, state in self.lists.items() if state.loaded}
        try:
            for state in self.lists.values():
                await self._refresh_list(state)
        except CalDavAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from None
        except CalDavError as err:
            raise UpdateFailed(str(err)) from None
        changed = self._fire_events(before)
        if changed:
            self._version += 1
        return self.lists

    async def _refresh_list(self, state: ListState) -> None:
        if not state.loaded:
            await self._full_fetch(state)
            return
        if state.sync_token:
            try:
                result = await self.client.sync(state.path, state.sync_token)
            except SyncTokenInvalid:
                _LOGGER.debug("sync token invalid for %s; full refetch", state.path)
                await self._full_fetch(state)
                return
            except CalDavError as err:
                if isinstance(err, CalDavAuthError):
                    raise
                _LOGGER.debug("sync-collection failed (%s); falling back to ctag", err)
                state.sync_token = None
            else:
                for href in result.deleted:
                    state.tasks.pop(href, None)
                if result.changed:
                    for res in await self.client.multiget(state.path, result.changed):
                        self._store(state, res.href, res.etag, res.data)
                state.sync_token = result.token or state.sync_token
                return
        ctag, token = await self.client.collection_state(state.path)
        if ctag is None or ctag != state.ctag:
            await self._full_fetch(state)
        state.sync_token = token or state.sync_token

    async def _full_fetch(self, state: ListState) -> None:
        ctag, token = await self.client.collection_state(state.path)
        resources = await self.client.fetch_all(state.path)
        state.tasks = {}
        for res in resources:
            self._store(state, res.href, res.etag, res.data)
        state.ctag, state.sync_token, state.loaded = ctag, token, True

    def _store(self, state: ListState, href: str, etag: str | None, data: str | None) -> None:
        if not data:
            state.tasks.pop(href, None)
            return
        try:
            state.tasks[href] = Task.from_ical(href, etag, data, self.tz)
        except ValueError as err:
            _LOGGER.debug("skipping unparsable resource %s: %s", href, err)
            state.tasks.pop(href, None)

    def _fire_events(self, before: dict[str, dict[str, Task]]) -> bool:
        """Diff against the previous snapshot and fire events. Returns True if anything changed."""
        changed = False
        for path, state in self.lists.items():
            old = before.get(path)
            new = {t.uid: t for t in state.tasks.values()}
            if old is None:
                changed = True  # first load: baseline, no events
                continue
            if set(old) != set(new):
                changed = True
            entity_id = self.list_entity_id(path)
            for uid, task in new.items():
                prev = old.get(uid)
                if prev is not None and prev.etag == task.etag and prev.raw == task.raw:
                    continue
                changed = True
                base = {"uid": uid, "summary": task.summary, "list": entity_id}
                if prev is None:
                    self.hass.bus.async_fire(EVENT_TASK_ADDED, {**base, "assignees": self.assignees_of(task)})
                    added = self.assignees_of(task)
                else:
                    if task.completed and not prev.completed:
                        self.hass.bus.async_fire(
                            EVENT_TASK_COMPLETED,
                            {**base, "assignees": self.assignees_of(task), "recurring": False},
                        )
                    added = [p for p in self.assignees_of(task) if p not in self.assignees_of(prev)]
                if added:
                    self.hass.bus.async_fire(
                        EVENT_TASK_ASSIGNED,
                        {
                            **base,
                            "persons": added,
                            "tags": [self.person_to_tag[p] for p in added],
                            "assignees": self.assignees_of(task),
                        },
                    )
        return changed

    @property
    def version(self) -> int:
        return self._version

    # -- writes ----------------------------------------------------------------
    async def async_create(self, path: str, summary: str, mutate: Mutation) -> str:
        if path not in self.lists:
            raise HomeAssistantError(f"List {path} is not exposed by this integration")
        uid = str(uuid.uuid4()).upper()
        now = dt_util.utcnow()
        doc = ICalDocument.parse(new_task_ical(uid, summary, now, PRODID))
        mutate(TaskEditor(doc, self.tz))
        href = f"{path}{uid}.ics"
        await self._guard(self.client.put(href, doc.serialize(), etag=None, create=True))
        await self.async_refresh()
        return uid

    async def async_mutate(self, uid: str, mutate: Mutation) -> bool:
        """Read-modify-write one task with If-Match; retry once on 412.

        Returns False when the edit did not change anything.
        """
        found = self.find(uid)
        if found is None:
            await self.async_refresh()
            found = self.find(uid)
        if found is None:
            raise HomeAssistantError(f"Task {uid} not found")
        _, task = found
        raw, etag = task.raw, task.etag
        for attempt in (1, 2):
            doc = ICalDocument.parse(raw)
            editor = TaskEditor(doc, self.tz)
            mutate(editor)
            if not editor.changed:
                return False
            editor.touch(dt_util.utcnow())
            try:
                await self._guard(self.client.put(task.href, doc.serialize(), etag=etag))
                break
            except PreconditionFailed:
                if attempt == 2:
                    raise HomeAssistantError(
                        f"Task '{task.summary}' was changed elsewhere at the same time; please reload and try again"
                    ) from None
                _LOGGER.debug("412 on %s; refetching and retrying", task.href)
                fresh = await self._guard(self.client.get(task.href))
                raw, etag = fresh.data or raw, fresh.etag
        await self.async_refresh()
        return True

    async def async_delete(self, uid: str) -> None:
        found = self.find(uid)
        if found is None:
            raise HomeAssistantError(f"Task {uid} not found")
        _, task = found
        try:
            await self._guard(self.client.delete(task.href, etag=task.etag))
        except PreconditionFailed:
            fresh = await self._guard(self.client.get(task.href))
            await self._guard(self.client.delete(task.href, etag=fresh.etag))
        except HomeAssistantError as err:
            if not isinstance(err.__cause__, NotFound):
                raise
        await self.async_refresh()

    async def _guard(self, awaitable: Any) -> Any:
        """Translate client errors into HomeAssistantError (safe messages)."""
        try:
            return await awaitable
        except PreconditionFailed:
            raise
        except CalDavAuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(f"Nextcloud rejected the credentials: {err}") from err
        except CalDavError as err:
            raise HomeAssistantError(f"Nextcloud request failed: {err}") from err

    def fire_recurring_completed(self, task: Task) -> None:
        """HA advanced a recurring task in place; the diff can't see that as a completion."""
        self.hass.bus.async_fire(
            EVENT_TASK_COMPLETED,
            {
                "uid": task.uid,
                "summary": task.summary,
                "list": self.list_entity_id(next(p for p, s in self.lists.items() if task.href in s.tasks)),
                "assignees": self.assignees_of(task),
                "recurring": True,
            },
        )
