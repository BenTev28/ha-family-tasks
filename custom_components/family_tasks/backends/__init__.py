"""Service adapters. Register new backends in BACKENDS."""

from __future__ import annotations

from .base import BackendSpec
from .caldav import CalDavSpec

BACKENDS: dict[str, BackendSpec] = {spec.key: spec for spec in (CalDavSpec(),)}

CONF_BACKEND = "backend"


def get_spec(key: str) -> BackendSpec:
    return BACKENDS[key]
