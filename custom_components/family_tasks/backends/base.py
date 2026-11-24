"""The contract every service adapter implements.

The integration's public surface (entities, actions, events, websocket API,
card) is fixed.  A backend only maps that surface onto one service.  See
docs/ARCHITECTURE.md for the rules.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, ClassVar

from homeassistant.core import HomeAssistant
import voluptuous as vol

from ..model import Task, TaskChange, WriteContext

# ---------------------------------------------------------------------------
# errors (messages must be safe to log: no URLs, tokens, passwords, bodies)


class BackendError(Exception):
    """Generic service failure."""


class BackendAuthError(BackendError):
    """Credentials rejected or expired; triggers reauth."""


class BackendConnectionError(BackendError):
    """Service unreachable (DNS, TCP, TLS, timeout)."""


class ConflictError(BackendError):
    """The task changed elsewhere and the change could not be re-applied."""


class TaskNotFoundError(BackendError):
    """The task no longer exists on the service."""


# ---------------------------------------------------------------------------
# capabilities


class Support(StrEnum):
    """How a backend provides a feature."""

    NATIVE = "native"  # stored on the service; other clients see it
    EMULATED = "emulated"  # kept by the integration (HA only); other clients don't see it
    NONE = "none"  # not available; actions setting it are rejected


@dataclass(frozen=True)
class Capabilities:
    """What a backend supports. Sent to the card so it can hide controls."""

    assignees: Support
    priority: Support
    recurrence: Support
    description: Support = Support.NATIVE
    due_time: bool = True  # due can carry a time of day
    subtasks: bool = False  # parent_id is reported
    conflict_detection: bool = False  # concurrent edits are detected, not overwritten

    def as_dict(self) -> dict[str, Any]:
        return {k: (v.value if isinstance(v, Support) else v) for k, v in asdict(self).items()}


@dataclass
class ListInfo:
    """A task list the user can pick."""

    id: str
    name: str


# ---------------------------------------------------------------------------
# the adapter


class TaskBackend(ABC):
    """One connected account on one service."""

    capabilities: ClassVar[Capabilities]

    @abstractmethod
    async def async_list_lists(self) -> list[ListInfo]:
        """Return the task lists available to this account."""

    @abstractmethod
    async def async_fetch(self, list_id: str) -> dict[str, Task]:
        """Return the current tasks of a list, keyed by task id.

        Backends should sync incrementally where the service allows it and
        keep their own cursors; the integration only sees full snapshots.
        """

    @abstractmethod
    async def async_create(self, list_id: str, summary: str, change: TaskChange, ctx: WriteContext) -> str:
        """Create a task, apply ``change`` and return the new task id."""

    @abstractmethod
    async def async_update(self, list_id: str, task_id: str, change: TaskChange, ctx: WriteContext) -> bool:
        """Apply a partial change. Return False if nothing changed.

        Must not overwrite concurrent edits silently: re-read and re-apply,
        or raise ConflictError.  Completing a recurring task must follow the
        backend's documented recurrence rule.
        """

    @abstractmethod
    async def async_delete(self, list_id: str, task_id: str) -> None:
        """Delete a task."""

    async def async_close(self) -> None:  # noqa: B027 - optional hook, default no-op
        """Release resources on unload."""


# ---------------------------------------------------------------------------
# how a backend plugs into setup


class BackendSpec(ABC):
    """Static description of a backend for the config flow and setup."""

    key: ClassVar[str]  # stored in entry.data["backend"]
    name: ClassVar[str]  # shown in the service picker
    capabilities: ClassVar[Capabilities]
    redact: ClassVar[frozenset[str]] = frozenset()  # entry.data keys to redact in diagnostics
    placeholders: ClassVar[dict[str, str]] = {}  # description placeholders for its connect step
    # The connect step is "connect_<key>" so each backend has its own form texts;
    # add a matching async_step_connect_<key> to the config flow.

    @abstractmethod
    def credentials_schema(self, defaults: Mapping[str, Any], *, reconfigure: bool = False) -> vol.Schema:
        """Schema for the connection step (empty schema if none needed)."""

    @abstractmethod
    async def async_validate(self, hass: HomeAssistant, data: dict[str, Any]) -> tuple[dict[str, str], list[ListInfo]]:
        """Check connection data. Return (errors, lists). May normalise ``data``."""

    @abstractmethod
    def unique_id(self, data: Mapping[str, Any]) -> str | None:
        """Account identity, to stop the same account being added twice."""

    @abstractmethod
    def title(self, data: Mapping[str, Any]) -> str:
        """Config entry title."""

    @abstractmethod
    def create(self, hass: HomeAssistant, data: Mapping[str, Any]) -> TaskBackend:
        """Build the live backend for a config entry."""
