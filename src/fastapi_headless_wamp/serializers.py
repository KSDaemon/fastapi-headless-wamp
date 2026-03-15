"""Pluggable serialization system for WAMP messages."""

from typing import Protocol, runtime_checkable


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
        import json

        return json.dumps(data)

    def decode(self, data: str | bytes) -> list[object]:
        import json

        result: list[object] = json.loads(data)
        return result
