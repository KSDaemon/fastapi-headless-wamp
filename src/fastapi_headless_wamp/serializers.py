"""Pluggable serialization system for WAMP messages."""

import json
import logging
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

logger = logging.getLogger(__name__)

# WAMP subprotocol prefix
WAMP_SUBPROTOCOL_PREFIX = "wamp.2."


@runtime_checkable
class Serializer(Protocol):
    """Protocol for WAMP message serializers."""

    @property
    def protocol(self) -> str: ...

    @property
    def is_binary(self) -> bool: ...

    def encode(self, data: list[object]) -> str | bytes: ...

    def decode(self, data: str | bytes) -> list[object]: ...


def _json_default(obj: Any) -> Any:
    """Fallback handler for ``json.dumps`` — supports common types.

    * **Pydantic models** — calls ``model_dump()`` (dict with Python types,
      then recursively serialized).
    * **dataclasses** — calls ``__dict__``.
    * ``datetime``, ``date``, ``time`` — ISO 8601 string.
    * ``Decimal`` — float.
    * ``UUID`` — string.
    * ``set`` / ``frozenset`` — list.
    * ``bytes`` — latin-1 string.
    """
    # Pydantic v2
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    # dataclass
    if hasattr(obj, "__dataclass_fields__"):
        return obj.__dict__
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    if isinstance(obj, bytes):
        return obj.decode("latin-1")
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class JsonSerializer:
    """JSON serializer for WAMP messages.

    Uses :func:`_json_default` as the fallback handler so that Pydantic
    models, dataclasses, ``datetime``, ``Decimal``, ``UUID``, and other
    common types can be serialized without explicit conversion.
    """

    @property
    def protocol(self) -> str:
        return "json"

    @property
    def is_binary(self) -> bool:
        return False

    def encode(self, data: list[object]) -> str:
        return json.dumps(data, default=_json_default)

    def decode(self, data: str | bytes) -> list[object]:
        result: list[object] = json.loads(data)
        return result


# ---------------------------------------------------------------------------
# Global serializer registry
# ---------------------------------------------------------------------------

_registry: dict[str, Serializer] = {}


def register_serializer(serializer: Serializer) -> None:
    """Register a serializer in the global registry.

    The serializer is keyed by its ``protocol`` property.  If a serializer
    with the same protocol name is already registered it will be replaced.
    """
    protocol = serializer.protocol
    _registry[protocol] = serializer
    logger.debug("Registered serializer for protocol '%s'", protocol)


def get_serializer(protocol: str) -> Serializer:
    """Return the serializer registered under *protocol*.

    Raises ``KeyError`` if no serializer is registered for the given protocol.
    """
    return _registry[protocol]


def get_available_subprotocols() -> list[str]:
    """Return WAMP subprotocol strings for all registered serializers.

    Each string has the form ``wamp.2.<protocol>`` (e.g. ``wamp.2.json``).
    """
    return [f"{WAMP_SUBPROTOCOL_PREFIX}{p}" for p in _registry]


# ---------------------------------------------------------------------------
# Register default serializers on module import
# ---------------------------------------------------------------------------

register_serializer(JsonSerializer())
