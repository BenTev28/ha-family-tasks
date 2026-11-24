"""CalDAV backend (Nextcloud, Radicale, Fastmail, iCloud, Baïkal, ...).

Mapping
- assignees -> VTODO CATEGORIES (native: phones and other clients see them)
- priority  -> PRIORITY 1 / 5 / 9 (Tasks.org / Nextcloud Tasks convention)
- recurrence-> RRULE; completing advances the task in place like Tasks.org
- conflicts -> If-Match on ETag, re-read and re-apply once on 412
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import logging
from typing import Any
from urllib.parse import urlsplit
import uuid

from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from ...const import PRODID
from ...model import UNSET, Task, TaskChange, WriteContext
from ..base import (
    BackendAuthError,
    BackendConnectionError,
    BackendError,
    BackendSpec,
    Capabilities,
    ConflictError,
    ListInfo,
    Support,
    TaskBackend,
    TaskNotFoundError,
)
from .client import (
    CalDavAuthError,
    CalDavClient,
    CalDavConnectionError,
    CalDavError,
    NotFound,
    PreconditionFailed,
    SyncTokenInvalid,
)
from .ics import ICalDocument
from .vtodo import PRIORITY_TO_ICAL, TaskEditor, VTodo, new_task_ical

_LOGGER = logging.getLogger(__name__)

CAPABILITIES = Capabilities(
    assignees=Support.NATIVE,
    priority=Support.NATIVE,
    recurrence=Support.NATIVE,
    description=Support.NATIVE,
    due_time=True,
    subtasks=True,
    conflict_detection=True,
)


class InvalidUrl(Exception):
    """URL is not an https URL without credentials."""


def validate_url(url: str) -> str:
    """Require https, a host, and no embedded credentials."""
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise InvalidUrl
    if parts.username or parts.password or "@" in parts.netloc:
        raise InvalidUrl
    if parts.query or parts.fragment:
        raise InvalidUrl
    return url.rstrip("/")


def _translate(err: CalDavError) -> BackendError:
    if isinstance(err, CalDavAuthError):
        return BackendAuthError(str(err))
    if isinstance(err, CalDavConnectionError):
        return BackendConnectionError(str(err))
    if isinstance(err, NotFound):
        return TaskNotFoundError(str(err))
    if isinstance(err, PreconditionFailed):
        return ConflictError(str(err))
    return BackendError(str(err))


@dataclass
class _ListState:
    path: str
    items: dict[str, VTodo] = field(default_factory=dict)  # href -> VTodo
    ctag: str | None = None
    sync_token: str | None = None
    loaded: bool = False


class CalDavBackend(TaskBackend):
    """One CalDAV account."""

    capabilities = CAPABILITIES

    def __init__(self, client: CalDavClient, tz: Any) -> None:
        self.client = client
        self._tz = tz
        self._lists: dict[str, _ListState] = {}

    # -- reading -------------------------------------------------------------
    async def async_list_lists(self) -> list[ListInfo]:
        try:
            return [ListInfo(c.path, c.name) for c in await self.client.discover()]
        except CalDavError as err:
            raise _translate(err) from None

    async def async_fetch(self, list_id: str) -> dict[str, Task]:
        state = self._lists.setdefault(list_id, _ListState(list_id))
        try:
            await self._refresh(state)
        except CalDavError as err:
            raise _translate(err) from None
        return {v.uid: v.to_task(list_id) for v in state.items.values()}

    async def _refresh(self, state: _ListState) -> None:
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
            except CalDavAuthError:
                raise
            except CalDavError as err:
                _LOGGER.debug("sync-collection failed (%s); falling back to ctag", err)
                state.sync_token = None
            else:
                for href in result.deleted:
                    state.items.pop(href, None)
                if result.changed:
                    for res in await self.client.multiget(state.path, result.changed):
                        self._store(state, res.href, res.etag, res.data)
                state.sync_token = result.token or state.sync_token
                return
        ctag, token = await self.client.collection_state(state.path)
        if ctag is None or ctag != state.ctag:
            await self._full_fetch(state)
        state.sync_token = token or state.sync_token

    async def _full_fetch(self, state: _ListState) -> None:
        ctag, token = await self.client.collection_state(state.path)
        resources = await self.client.fetch_all(state.path)
        state.items = {}
        for res in resources:
            self._store(state, res.href, res.etag, res.data)
        state.ctag, state.sync_token, state.loaded = ctag, token, True

    def _store(self, state: _ListState, href: str, etag: str | None, data: str | None) -> None:
        if not data:
            state.items.pop(href, None)
            return
        try:
            state.items[href] = VTodo.from_ical(href, etag, data, self._tz)
        except ValueError as err:
            _LOGGER.debug("skipping unparsable resource %s: %s", href, err)
            state.items.pop(href, None)

    def _find(self, list_id: str, task_id: str) -> VTodo:
        state = self._lists.get(list_id)
        if state is not None:
            for item in state.items.values():
                if item.uid == task_id:
                    return item
        raise TaskNotFoundError(f"task {task_id} not found")

    # -- writing ---------------------------------------------------------------
    async def async_create(self, list_id: str, summary: str, change: TaskChange, ctx: WriteContext) -> str:
        uid = str(uuid.uuid4()).upper()
        doc = ICalDocument.parse(new_task_ical(uid, summary, ctx.now, PRODID))
        editor = TaskEditor(doc, ctx.tz)
        apply_change(editor, change, ctx)
        try:
            await self.client.put(f"{list_id}{uid}.ics", doc.serialize(), etag=None, create=True)
        except CalDavError as err:
            raise _translate(err) from None
        return uid

    async def async_update(self, list_id: str, task_id: str, change: TaskChange, ctx: WriteContext) -> bool:
        item = self._find(list_id, task_id)
        raw, etag = item.raw, item.etag
        for attempt in (1, 2):
            doc = ICalDocument.parse(raw)
            editor = TaskEditor(doc, ctx.tz)
            apply_change(editor, change, ctx)
            if not editor.changed:
                return False
            editor.touch(ctx.now)
            try:
                await self.client.put(item.href, doc.serialize(), etag=etag)
                return True
            except PreconditionFailed:
                if attempt == 2:
                    raise ConflictError("changed elsewhere at the same time") from None
                _LOGGER.debug("412 on %s; refetching and retrying", item.href)
                try:
                    fresh = await self.client.get(item.href)
                except CalDavError as err:
                    raise _translate(err) from None
                raw, etag = fresh.data or raw, fresh.etag
            except CalDavError as err:
                raise _translate(err) from None
        return True  # pragma: no cover

    async def async_delete(self, list_id: str, task_id: str) -> None:
        item = self._find(list_id, task_id)
        try:
            try:
                await self.client.delete(item.href, etag=item.etag)
            except PreconditionFailed:
                fresh = await self.client.get(item.href)
                await self.client.delete(item.href, etag=fresh.etag)
        except NotFound:
            return
        except CalDavError as err:
            raise _translate(err) from None


def apply_change(editor: TaskEditor, change: TaskChange, ctx: WriteContext) -> None:
    """Map a neutral TaskChange onto VTODO edits."""
    if change.summary is not UNSET:
        editor.set_summary(change.summary)  # type: ignore[arg-type]
    if change.description is not UNSET:
        editor.set_description(change.description)  # type: ignore[arg-type]
    if change.priority is not UNSET:
        editor.set_priority(PRIORITY_TO_ICAL[change.priority] if change.priority else None)  # type: ignore[index]
    if change.rrule is not UNSET:
        editor.set_rrule(change.rrule)  # type: ignore[arg-type]
    if change.due is not UNSET:
        editor.set_due(change.due)  # type: ignore[arg-type]
    if change.assignees is not None:
        current = VTodo.from_ical("", None, editor.doc.serialize(), editor.tz).categories
        editor.set_assignee_tags(change.assignees.resolve(current, ctx.managed_tags), ctx.managed_tags)
    if change.completed is not None:
        is_done = VTodo.from_ical("", None, editor.doc.serialize(), editor.tz).completed
        if change.completed and not is_done:
            editor.complete(ctx.now)
        elif not change.completed and is_done:
            editor.reopen()


# ---------------------------------------------------------------------------
# setup


class CalDavSpec(BackendSpec):
    """CalDAV server with username + (app) password."""

    key = "caldav"
    name = "CalDAV (Nextcloud, Radicale, Fastmail, iCloud, …)"
    capabilities = CAPABILITIES
    redact = frozenset({CONF_URL, CONF_USERNAME, CONF_PASSWORD})
    placeholders = {"example_url": "https://cloud.example.com/remote.php/dav"}

    def credentials_schema(self, defaults: Mapping[str, Any], *, reconfigure: bool = False) -> vol.Schema:
        password = vol.Optional(CONF_PASSWORD) if reconfigure else vol.Required(CONF_PASSWORD)
        return vol.Schema(
            {
                vol.Required(CONF_URL, default=defaults.get(CONF_URL, vol.UNDEFINED)): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                ),
                vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, vol.UNDEFINED)): str,
                password: selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
                vol.Required(CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)): bool,
            }
        )

    async def async_validate(self, hass: HomeAssistant, data: dict[str, Any]) -> tuple[dict[str, str], list[ListInfo]]:
        try:
            data[CONF_URL] = validate_url(data[CONF_URL])
        except InvalidUrl:
            return {CONF_URL: "invalid_url"}, []
        try:
            lists = await self.create(hass, data).async_list_lists()
        except BackendAuthError:
            return {"base": "invalid_auth"}, []
        except BackendConnectionError:
            return {"base": "cannot_connect"}, []
        except BackendError as err:
            _LOGGER.debug("discovery failed: %s", err)
            return {"base": "not_caldav"}, []
        if not lists:
            return {"base": "no_task_lists"}, []
        return {}, lists

    def unique_id(self, data: Mapping[str, Any]) -> str | None:
        return f"caldav:{data[CONF_USERNAME].lower()}@{urlsplit(data[CONF_URL]).hostname}"

    def title(self, data: Mapping[str, Any]) -> str:
        return f"{data[CONF_USERNAME]} @ {urlsplit(data[CONF_URL]).hostname}"

    def create(self, hass: HomeAssistant, data: Mapping[str, Any]) -> CalDavBackend:
        from zoneinfo import ZoneInfo

        verify = data.get(CONF_VERIFY_SSL, True)
        client = CalDavClient(
            async_get_clientsession(hass, verify_ssl=verify),
            data[CONF_URL],
            data[CONF_USERNAME],
            data[CONF_PASSWORD],
            verify,
        )
        return CalDavBackend(client, ZoneInfo(hass.config.time_zone))
