"""Tests for the pluggable serialization system (US-003)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import BaseModel

from fastapi_headless_wamp.serializers import (
    WAMP_SUBPROTOCOL_PREFIX,
    CborSerializer,
    JsonSerializer,
    MsgpackSerializer,
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
            get_serializer("flatbuffers")

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

        class FlatbufSerializer:
            @property
            def protocol(self) -> str:
                return "flatbuf"

            @property
            def is_binary(self) -> bool:
                return True

            def encode(self, data: list[object]) -> bytes:
                return b""

            def decode(self, data: str | bytes) -> list[object]:
                return []

        register_serializer(FlatbufSerializer())
        subprotocols = get_available_subprotocols()
        assert "wamp.2.flatbuf" in subprotocols
        assert "wamp.2.json" in subprotocols


# ---------------------------------------------------------------------------
# JsonSerializer: _json_default fallback tests
# ---------------------------------------------------------------------------


class _SampleModel(BaseModel):
    name: str
    value: int


@dataclass
class _SampleDC:
    x: int
    y: str


class TestJsonDefaultFallback:
    """JsonSerializer handles Pydantic models, dataclasses, and common types."""

    def test_pydantic_model(self) -> None:
        s = JsonSerializer()
        msg: list[object] = [50, 1, {}, [_SampleModel(name="test", value=42)]]
        encoded = s.encode(msg)
        decoded = json.loads(encoded)
        assert decoded[3][0] == {"name": "test", "value": 42}

    def test_dataclass(self) -> None:
        s = JsonSerializer()
        msg: list[object] = [50, 1, {}, [_SampleDC(x=10, y="hello")]]
        encoded = s.encode(msg)
        decoded = json.loads(encoded)
        assert decoded[3][0] == {"x": 10, "y": "hello"}

    def test_datetime(self) -> None:
        s = JsonSerializer()
        dt = datetime(2025, 6, 15, 12, 30, 0, tzinfo=timezone.utc)
        msg: list[object] = [50, 1, {}, [{"ts": dt}]]
        encoded = s.encode(msg)
        decoded = json.loads(encoded)
        assert decoded[3][0]["ts"] == "2025-06-15T12:30:00+00:00"

    def test_decimal(self) -> None:
        s = JsonSerializer()
        msg: list[object] = [50, 1, {}, [{"price": Decimal("19.99")}]]
        encoded = s.encode(msg)
        decoded = json.loads(encoded)
        assert decoded[3][0]["price"] == 19.99

    def test_uuid(self) -> None:
        s = JsonSerializer()
        uid = UUID("12345678-1234-5678-1234-567812345678")
        msg: list[object] = [50, 1, {}, [{"id": uid}]]
        encoded = s.encode(msg)
        decoded = json.loads(encoded)
        assert decoded[3][0]["id"] == "12345678-1234-5678-1234-567812345678"

    def test_set_and_frozenset(self) -> None:
        s = JsonSerializer()
        msg: list[object] = [50, 1, {}, [{"tags": frozenset(["a", "b"])}]]
        encoded = s.encode(msg)
        decoded = json.loads(encoded)
        assert sorted(decoded[3][0]["tags"]) == ["a", "b"]

    def test_unsupported_type_raises(self) -> None:
        s = JsonSerializer()
        msg: list[object] = [50, 1, {}, [{"bad": object()}]]
        with pytest.raises(TypeError, match="not JSON serializable"):
            s.encode(msg)


# ---------------------------------------------------------------------------
# CborSerializer tests
# ---------------------------------------------------------------------------


class TestCborSerializer:
    def test_protocol_name(self) -> None:
        s = CborSerializer()
        assert s.protocol == "cbor"

    def test_is_binary(self) -> None:
        s = CborSerializer()
        assert s.is_binary is True

    def test_encode_returns_bytes(self) -> None:
        s = CborSerializer()
        msg: list[object] = [1, "realm1", {"roles": {"caller": {}}}]
        encoded = s.encode(msg)
        assert isinstance(encoded, bytes)

    def test_encode_decode_roundtrip(self) -> None:
        s = CborSerializer()
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

    def test_decode_from_bytes(self) -> None:
        s = CborSerializer()
        msg: list[object] = [48, 1, {}, "com.example.add", [2, 3]]
        encoded = s.encode(msg)
        decoded = s.decode(encoded)
        assert decoded == msg

    def test_encode_empty_message(self) -> None:
        s = CborSerializer()
        encoded = s.encode([])
        decoded = s.decode(encoded)
        assert decoded == []

    def test_implements_serializer_protocol(self) -> None:
        s = CborSerializer()
        assert isinstance(s, Serializer)

    def test_pydantic_model(self) -> None:
        s = CborSerializer()
        msg: list[object] = [50, 1, {}, [_SampleModel(name="test", value=42)]]
        encoded = s.encode(msg)
        decoded = s.decode(encoded)
        assert decoded[3][0] == {"name": "test", "value": 42}

    def test_dataclass(self) -> None:
        s = CborSerializer()
        msg: list[object] = [50, 1, {}, [_SampleDC(x=10, y="hello")]]
        encoded = s.encode(msg)
        decoded = s.decode(encoded)
        assert decoded[3][0] == {"x": 10, "y": "hello"}

    def test_datetime_native(self) -> None:
        """CBOR preserves datetime as a native tagged value."""
        s = CborSerializer()
        dt = datetime(2025, 6, 15, 12, 30, 0, tzinfo=timezone.utc)
        msg: list[object] = [50, 1, {}, [{"ts": dt}]]
        encoded = s.encode(msg)
        decoded = s.decode(encoded)
        assert decoded[3][0]["ts"] == dt

    def test_registered_by_default(self) -> None:
        """CborSerializer is auto-registered when cbor2 is available."""
        s = get_serializer("cbor")
        assert isinstance(s, CborSerializer)

    def test_available_in_subprotocols(self) -> None:
        subprotocols = get_available_subprotocols()
        assert "wamp.2.cbor" in subprotocols


# ---------------------------------------------------------------------------
# MsgpackSerializer tests
# ---------------------------------------------------------------------------


class TestMsgpackSerializer:
    def test_protocol_name(self) -> None:
        s = MsgpackSerializer()
        assert s.protocol == "msgpack"

    def test_is_binary(self) -> None:
        s = MsgpackSerializer()
        assert s.is_binary is True

    def test_encode_returns_bytes(self) -> None:
        s = MsgpackSerializer()
        msg: list[object] = [1, "realm1", {"roles": {"caller": {}}}]
        encoded = s.encode(msg)
        assert isinstance(encoded, bytes)

    def test_encode_decode_roundtrip(self) -> None:
        s = MsgpackSerializer()
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

    def test_decode_from_bytes(self) -> None:
        s = MsgpackSerializer()
        msg: list[object] = [48, 1, {}, "com.example.add", [2, 3]]
        encoded = s.encode(msg)
        decoded = s.decode(encoded)
        assert decoded == msg

    def test_encode_empty_message(self) -> None:
        s = MsgpackSerializer()
        encoded = s.encode([])
        decoded = s.decode(encoded)
        assert decoded == []

    def test_implements_serializer_protocol(self) -> None:
        s = MsgpackSerializer()
        assert isinstance(s, Serializer)

    def test_pydantic_model(self) -> None:
        s = MsgpackSerializer()
        msg: list[object] = [50, 1, {}, [_SampleModel(name="test", value=42)]]
        encoded = s.encode(msg)
        decoded = s.decode(encoded)
        assert decoded[3][0] == {"name": "test", "value": 42}

    def test_dataclass(self) -> None:
        s = MsgpackSerializer()
        msg: list[object] = [50, 1, {}, [_SampleDC(x=10, y="hello")]]
        encoded = s.encode(msg)
        decoded = s.decode(encoded)
        assert decoded[3][0] == {"x": 10, "y": "hello"}

    def test_datetime_as_isoformat(self) -> None:
        """MsgPack converts datetime to ISO string (unlike CBOR which preserves it)."""
        s = MsgpackSerializer()
        dt = datetime(2025, 6, 15, 12, 30, 0, tzinfo=timezone.utc)
        msg: list[object] = [50, 1, {}, [{"ts": dt}]]
        encoded = s.encode(msg)
        decoded = s.decode(encoded)
        assert decoded[3][0]["ts"] == "2025-06-15T12:30:00+00:00"

    def test_decimal(self) -> None:
        s = MsgpackSerializer()
        msg: list[object] = [50, 1, {}, [{"price": Decimal("19.99")}]]
        encoded = s.encode(msg)
        decoded = s.decode(encoded)
        assert decoded[3][0]["price"] == 19.99

    def test_uuid(self) -> None:
        s = MsgpackSerializer()
        uid = UUID("12345678-1234-5678-1234-567812345678")
        msg: list[object] = [50, 1, {}, [{"id": uid}]]
        encoded = s.encode(msg)
        decoded = s.decode(encoded)
        assert decoded[3][0]["id"] == "12345678-1234-5678-1234-567812345678"

    def test_set_and_frozenset(self) -> None:
        s = MsgpackSerializer()
        msg: list[object] = [50, 1, {}, [{"tags": frozenset(["a", "b"])}]]
        encoded = s.encode(msg)
        decoded = s.decode(encoded)
        assert sorted(decoded[3][0]["tags"]) == ["a", "b"]

    def test_unsupported_type_raises(self) -> None:
        s = MsgpackSerializer()
        msg: list[object] = [50, 1, {}, [{"bad": object()}]]
        with pytest.raises(TypeError, match="not MsgPack serializable"):
            s.encode(msg)

    def test_registered_by_default(self) -> None:
        """MsgpackSerializer is auto-registered when msgpack is available."""
        s = get_serializer("msgpack")
        assert isinstance(s, MsgpackSerializer)

    def test_available_in_subprotocols(self) -> None:
        subprotocols = get_available_subprotocols()
        assert "wamp.2.msgpack" in subprotocols
