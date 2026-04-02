"""Pluggable serialization system for WAMP messages."""

import contextlib
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
# CBOR serializer (optional — requires ``cbor2``)
# ---------------------------------------------------------------------------


def _cbor_default(encoder: Any, obj: Any) -> None:
    """Fallback for cbor2 — handles Pydantic models and dataclasses.

    ``cbor2`` already natively supports ``datetime``, ``date``, ``Decimal``,
    ``UUID``, ``set``, ``frozenset``, and ``bytes``, so only Pydantic models
    and dataclasses need custom handling here.
    """
    if hasattr(obj, "model_dump"):
        encoder.encode(obj.model_dump())
    elif hasattr(obj, "__dataclass_fields__"):
        encoder.encode(obj.__dict__)
    else:
        raise TypeError(f"Object of type {type(obj).__name__} is not CBOR serializable")


class CborSerializer:
    """CBOR serializer for WAMP messages (``wamp.2.cbor``).

    Requires the ``cbor2`` package::

        pip install fastapi-headless-wamp[cbor]

    CBOR natively supports ``datetime``, ``Decimal``, ``UUID``, ``bytes``
    and other types that JSON cannot represent without string conversion.
    """

    def __init__(self) -> None:
        try:
            import cbor2 as _cbor2
        except ImportError as exc:
            raise ImportError(
                "cbor2 is required for CBOR serialization. Install it with: pip install fastapi-headless-wamp[cbor]"
            ) from exc
        self._cbor2 = _cbor2

    @property
    def protocol(self) -> str:
        return "cbor"

    @property
    def is_binary(self) -> bool:
        return True

    def encode(self, data: list[object]) -> bytes:
        return self._cbor2.dumps(data, default=_cbor_default)

    def decode(self, data: str | bytes) -> list[object]:
        if isinstance(data, str):
            data = data.encode("utf-8")
        result: list[object] = self._cbor2.loads(data)
        return result


# ---------------------------------------------------------------------------
# MsgPack serializer (optional — requires ``msgpack``)
# ---------------------------------------------------------------------------


def _msgpack_default(obj: Any) -> Any:
    """Fallback for msgpack — handles Pydantic models, dataclasses, and common types.

    ``msgpack`` only natively supports ``int``, ``float``, ``str``, ``bytes``,
    ``list``, ``dict``, ``bool``, and ``None``.  Everything else needs
    explicit conversion here.
    """
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
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
    raise TypeError(f"Object of type {type(obj).__name__} is not MsgPack serializable")


class MsgpackSerializer:
    """MsgPack serializer for WAMP messages (``wamp.2.msgpack``).

    Requires the ``msgpack`` package::

        pip install fastapi-headless-wamp[msgpack]

    MsgPack is a compact binary format that is faster and smaller than JSON
    while supporting the same data types.
    """

    def __init__(self) -> None:
        try:
            import msgpack as _msgpack
        except ImportError as exc:
            raise ImportError(
                "msgpack is required for MsgPack serialization. "
                "Install it with: pip install fastapi-headless-wamp[msgpack]"
            ) from exc
        self._msgpack = _msgpack

    @property
    def protocol(self) -> str:
        return "msgpack"

    @property
    def is_binary(self) -> bool:
        return True

    def encode(self, data: list[object]) -> bytes:
        return self._msgpack.packb(data, default=_msgpack_default, use_bin_type=True)

    def decode(self, data: str | bytes) -> list[object]:
        if isinstance(data, str):
            data = data.encode("utf-8")
        result: list[object] = self._msgpack.unpackb(data, raw=False)
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

with contextlib.suppress(ImportError):
    register_serializer(CborSerializer())

with contextlib.suppress(ImportError):
    register_serializer(MsgpackSerializer())
