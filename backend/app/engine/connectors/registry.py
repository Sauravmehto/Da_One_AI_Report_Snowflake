"""Connector registry: vendor/dialect name -> DBConnector instance.

Each connector package registers itself on import (see
connectors/snowflake/__init__.py). This file should never need editing to
add a new vendor — that is the whole point of the registry existing.
"""

from __future__ import annotations

from app.engine.connectors.base import DBConnector

_REGISTRY: dict[str, DBConnector] = {}


def register(name: str, connector: DBConnector) -> None:
    """Register a connector under `name` (e.g. "snowflake"). Raises if that
    name is already registered — a silent overwrite would hide a real bug
    (two connectors claiming the same name)."""
    if name in _REGISTRY:
        raise ValueError(f"connector '{name}' is already registered")
    _REGISTRY[name] = connector


def get(name: str) -> DBConnector:
    """The registered connector for `name`. Raises KeyError with the
    available names if it hasn't been registered — usually because its
    package (e.g. connectors.snowflake) hasn't been imported yet."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"No connector registered for '{name}'. Available: {sorted(_REGISTRY) or '(none imported yet)'}. "
            f"Import the connector package first, e.g. `import connectors.snowflake`."
        ) from None


def available() -> list[str]:
    return sorted(_REGISTRY)
