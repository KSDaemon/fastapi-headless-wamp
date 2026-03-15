"""Tests for the pluggable serialization system (US-003)."""

from __future__ import annotations

import json

import pytest

from fastapi_headless_wamp.serializers import (
    WAMP_SUBPROTOCOL_PREFIX,
    JsonSerializer,
    Serializer,
    get_available_subprotocols,
    get_serializer,
    register_serializer,
)


# ---------------------------------------------------------------------------
# JsonSerializer tests
# ---------------------------------------------------------------------------


class TestJsonSerializer:
    def test_protocol_name(self) -> None:
        s = JsonSerializer()
        assert s.protocol == "json"

    def test_is_not_binary(self) -> None:
        s = JsonSerializer()
        assert s.is_binary is False

    def test_encode_wamp_message(self) -> None:
        s = JsonSerializer()
        # A WAMP HELLO message: [1, "realm1", {…}]
        msg: list[object] = [1, "realm1", {"roles": {"caller": {}}}]
        encoded = s.encode(msg)
        assert isinstance(encoded, str)
        assert json.loads(encoded) == msg

    def test_decode_wamp_message_from_str(self) -> None:
        s = JsonSerializer()
        raw = '[48, 1, {}, "com.example.add", [2, 3]]'
        decoded = s.decode(raw)
        assert decoded == [48, 1, {}, "com.example.add", [2, 3]]

    def test_decode_wamp_message_from_bytes(self) -> None:
        s = JsonSerializer()
        raw = b"[50, 1, {}, [5]]"
        decoded = s.decode(raw)
        assert decoded == [50, 1, {}, [5]]

    def test_encode_decode_roundtrip(self) -> None:
        s = JsonSerializer()
        original: list[object] = [
            36,
            42,
            100,
            {},
            ["hello", "world"],
            {"key": "value"},
        ]
        encoded = s.encode(original)
        decoded = s.decode(encoded)
        assert decoded == original

    def test_encode_empty_message(self) -> None:
        s = JsonSerializer()
        encoded = s.encode([])
        assert json.loads(encoded) == []

    def test_implements_serializer_protocol(self) -> None:
        """JsonSerializer satisfies the Serializer runtime-checkable protocol."""
        s = JsonSerializer()
        assert isinstance(s, Serializer)


# ---------------------------------------------------------------------------
# Serializer registry tests
# ---------------------------------------------------------------------------


class TestSerializerRegistry:
    def test_json_registered_by_default(self) -> None:
        """JsonSerializer is registered at module import time."""
        s = get_serializer("json")
        assert isinstance(s, JsonSerializer)

    def test_get_serializer_returns_correct_instance(self) -> None:
        s = get_serializer("json")
        assert s.protocol == "json"
        assert s.is_binary is False

    def test_get_serializer_unknown_raises_key_error(self) -> None:
        with pytest.raises(KeyError):
            get_serializer("msgpack")

    def test_register_custom_serializer(self) -> None:
        """Register a custom serializer and retrieve it."""

        class DummySerializer:
            @property
            def protocol(self) -> str:
                return "dummy"

            @property
            def is_binary(self) -> bool:
                return True

            def encode(self, data: list[object]) -> bytes:
                return b"dummy:" + json.dumps(data).encode()

            def decode(self, data: str | bytes) -> list[object]:
                raw = data if isinstance(data, bytes) else data.encode()
                result: list[object] = json.loads(raw.split(b":", 1)[1])
                return result

        dummy = DummySerializer()
        register_serializer(dummy)
        retrieved = get_serializer("dummy")
        assert retrieved is dummy
        assert retrieved.protocol == "dummy"
        assert retrieved.is_binary is True

        # Verify encode/decode round-trip
        msg: list[object] = [1, "realm1", {}]
        encoded = retrieved.encode(msg)
        assert isinstance(encoded, bytes)
        decoded = retrieved.decode(encoded)
        assert decoded == msg

    def test_register_replaces_existing(self) -> None:
        """Re-registering the same protocol name replaces the old instance."""
        original = get_serializer("json")
        new_json = JsonSerializer()
        register_serializer(new_json)
        assert get_serializer("json") is new_json
        # Restore original to not pollute other tests
        register_serializer(original)


# ---------------------------------------------------------------------------
# Subprotocol string tests
# ---------------------------------------------------------------------------


class TestSubprotocols:
    def test_prefix_constant(self) -> None:
        assert WAMP_SUBPROTOCOL_PREFIX == "wamp.2."

    def test_available_subprotocols_includes_json(self) -> None:
        subprotocols = get_available_subprotocols()
        assert "wamp.2.json" in subprotocols

    def test_available_subprotocols_format(self) -> None:
        subprotocols = get_available_subprotocols()
        for sp in subprotocols:
            assert sp.startswith(WAMP_SUBPROTOCOL_PREFIX)

    def test_custom_serializer_appears_in_subprotocols(self) -> None:
        """After registering a custom serializer its subprotocol is listed."""

        class MsgpackSerializer:
            @property
            def protocol(self) -> str:
                return "msgpack"

            @property
            def is_binary(self) -> bool:
                return True

            def encode(self, data: list[object]) -> bytes:
                return b""

            def decode(self, data: str | bytes) -> list[object]:
                return []

        register_serializer(MsgpackSerializer())
        subprotocols = get_available_subprotocols()
        assert "wamp.2.msgpack" in subprotocols
        assert "wamp.2.json" in subprotocols
