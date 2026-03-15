"""Pluggable serialization system for WAMP messages."""

import json
import logging
from typing import Protocol, runtime_checkable

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


class JsonSerializer:
    """JSON serializer for WAMP messages."""

    @property
    def protocol(self) -> str:
        return "json"

    @property
    def is_binary(self) -> bool:
        return False

    def encode(self, data: list[object]) -> str:
        return json.dumps(data)

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
