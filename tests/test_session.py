"""Tests for WampSession core (US-005)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from fastapi_headless_wamp.errors import WampError
from fastapi_headless_wamp.serializers import JsonSerializer, Serializer
from fastapi_headless_wamp.session import WampSession

# ---------------------------------------------------------------------------
# Helpers: mock WebSocket that simulates Starlette's WebSocket interface
# ---------------------------------------------------------------------------


class MockWebSocket:
    """A minimal mock that behaves like starlette.websockets.WebSocket."""

    def __init__(self) -> None:
        self.sent_texts: list[str] = []
        self.sent_bytes: list[bytes] = []
        self._receive_text_queue: asyncio.Queue[str] = asyncio.Queue()
        self._receive_bytes_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._closed = False

    async def send_text(self, data: str) -> None:
        self.sent_texts.append(data)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def receive_text(self) -> str:
        return await self._receive_text_queue.get()

    async def receive_bytes(self) -> bytes:
        return await self._receive_bytes_queue.get()

    def enqueue_text(self, data: str) -> None:
        self._receive_text_queue.put_nowait(data)

    def enqueue_bytes(self, data: bytes) -> None:
        self._receive_bytes_queue.put_nowait(data)


def make_session(
    ws: MockWebSocket | None = None,
    serializer: Serializer | None = None,
) -> WampSession:
    """Create a WampSession with mocks for testing."""
    if ws is None:
        ws = MockWebSocket()
    if serializer is None:
        serializer = JsonSerializer()
    # Cast to Any to satisfy the type checker for mock
    return WampSession(ws, serializer)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Constructor and initial state
# ---------------------------------------------------------------------------


class TestWampSessionInit:
    def test_initial_state(self) -> None:
        session = make_session()
        assert session.session_id == 0
        assert session.realm == ""
        assert session.is_open is False
        assert session.server_rpcs == {}
        assert session.client_rpcs == {}
        assert session.client_rpc_uris == {}
        assert session.subscriptions == {}
        assert session.subscription_uris == {}
        assert session.server_subscriptions == {}
        assert session.pending_calls == {}

    def test_websocket_and_serializer_stored(self) -> None:
        ws = MockWebSocket()
        serializer = JsonSerializer()
        session = make_session(ws, serializer)
        assert session.websocket is ws  # type: ignore[comparison-overlap]
        assert session.serializer is serializer

    def test_request_id_counter_starts_at_zero(self) -> None:
        session = make_session()
        assert session._request_id_counter == 0

    def test_next_request_id_increments(self) -> None:
        session = make_session()
        assert session.next_request_id() == 1
        assert session.next_request_id() == 2
        assert session.next_request_id() == 3


# ---------------------------------------------------------------------------
# Session ID generation
# ---------------------------------------------------------------------------


class TestSessionIdGeneration:
    def test_generate_session_id_in_range(self) -> None:
        session = make_session()
        sid = session.generate_session_id()
        assert 1 <= sid <= 2**53
        assert session.session_id == sid

    def test_generate_session_id_varies(self) -> None:
        """Two calls should (almost certainly) produce different IDs."""
        session = make_session()
        id1 = session.generate_session_id()
        id2 = session.generate_session_id()
        # Statistically extremely unlikely to be equal
        # but we just verify they're in range
        assert 1 <= id1 <= 2**53
        assert 1 <= id2 <= 2**53


# ---------------------------------------------------------------------------
# send_message / receive_message
# ---------------------------------------------------------------------------


class TestSendReceiveJson:
    """Test send/receive with the JSON serializer (text mode)."""

    async def test_send_message_text_mode(self) -> None:
        ws = MockWebSocket()
        session = make_session(ws)
        msg = [1, "realm1", {"roles": {}}]
        await session.send_message(msg)
        assert len(ws.sent_texts) == 1
        assert len(ws.sent_bytes) == 0
        # Should be valid JSON
        import json

        assert json.loads(ws.sent_texts[0]) == msg

    async def test_receive_message_text_mode(self) -> None:
        ws = MockWebSocket()
        session = make_session(ws)
        ws.enqueue_text('[48, 1, {}, "com.add", [2, 3]]')
        msg = await session.receive_message()
        assert msg == [48, 1, {}, "com.add", [2, 3]]

    async def test_send_receive_roundtrip(self) -> None:
        """Send a message and verify we can decode what was sent."""
        ws = MockWebSocket()
        session = make_session(ws)
        original = [50, 1, {}, [42]]
        await session.send_message(original)

        import json

        decoded = json.loads(ws.sent_texts[0])
        assert decoded == original


class TestSendReceiveBinary:
    """Test send/receive with a binary serializer."""

    def _make_binary_serializer(self) -> Serializer:
        """Create a simple binary serializer that uses JSON bytes."""

        class BinaryJsonSerializer:
            @property
            def protocol(self) -> str:
                return "binary_json"

            @property
            def is_binary(self) -> bool:
                return True

            def encode(self, data: list[object]) -> bytes:
                import json

                return json.dumps(data).encode("utf-8")

            def decode(self, data: str | bytes) -> list[object]:
                import json

                raw = data if isinstance(data, bytes) else data.encode("utf-8")
                result: list[object] = json.loads(raw)
                return result

        return BinaryJsonSerializer()  # type: ignore[return-value]

    async def test_send_message_binary_mode(self) -> None:
        ws = MockWebSocket()
        serializer = self._make_binary_serializer()
        session = make_session(ws, serializer)
        msg = [1, "realm1", {}]
        await session.send_message(msg)
        assert len(ws.sent_bytes) == 1
        assert len(ws.sent_texts) == 0

    async def test_receive_message_binary_mode(self) -> None:
        ws = MockWebSocket()
        serializer = self._make_binary_serializer()
        session = make_session(ws, serializer)
        import json

        ws.enqueue_bytes(json.dumps([48, 1, {}, "com.add"]).encode("utf-8"))
        msg = await session.receive_message()
        assert msg == [48, 1, {}, "com.add"]


# ---------------------------------------------------------------------------
# Async iterator
# ---------------------------------------------------------------------------


class TestAsyncIterator:
    async def test_async_for_yields_messages(self) -> None:
        ws = MockWebSocket()
        session = make_session(ws)
        ws.enqueue_text("[1, 2, 3]")
        ws.enqueue_text("[4, 5, 6]")

        messages: list[list[Any]] = []
        count = 0
        async for msg in session:
            messages.append(msg)
            count += 1
            if count >= 2:
                break

        assert len(messages) == 2
        assert messages[0] == [1, 2, 3]
        assert messages[1] == [4, 5, 6]

    async def test_async_for_stops_on_exception(self) -> None:
        """When receive_message raises, the async iterator stops."""
        ws = MockWebSocket()
        session = make_session(ws)
        # Don't enqueue anything — the queue get will hang, so we use
        # a mock that raises
        session.receive_message = AsyncMock(  # type: ignore[method-assign]
            side_effect=Exception("connection closed")
        )

        messages: list[list[Any]] = [msg async for msg in session]

        assert messages == []


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


class TestCleanup:
    def test_cleanup_sets_is_open_false(self) -> None:
        session = make_session()
        session.is_open = True
        session.cleanup()
        assert session.is_open is False

    def test_cleanup_clears_maps(self) -> None:
        session = make_session()
        session.server_rpcs["test"] = lambda: None
        session.client_rpcs[1] = "com.test"
        session.client_rpc_uris["com.test"] = 1
        session.subscriptions[1] = "com.topic"
        session.subscription_uris["com.topic"] = 1
        session.server_subscriptions["com.topic"] = lambda: None

        session.cleanup()

        assert session.server_rpcs == {}
        assert session.client_rpcs == {}
        assert session.client_rpc_uris == {}
        assert session.subscriptions == {}
        assert session.subscription_uris == {}
        assert session.server_subscriptions == {}
        assert session.pending_calls == {}

    async def test_cleanup_rejects_pending_futures(self) -> None:
        session = make_session()
        loop = asyncio.get_event_loop()
        future1: asyncio.Future[Any] = loop.create_future()
        future2: asyncio.Future[Any] = loop.create_future()
        session.pending_calls[1] = future1
        session.pending_calls[2] = future2
        session.session_id = 42

        session.cleanup()

        with pytest.raises(WampError, match="disconnected"):
            future1.result()
        with pytest.raises(WampError, match="disconnected"):
            future2.result()

    async def test_cleanup_ignores_already_done_futures(self) -> None:
        session = make_session()
        loop = asyncio.get_event_loop()
        future: asyncio.Future[Any] = loop.create_future()
        future.set_result("already done")
        session.pending_calls[1] = future

        # Should not raise
        session.cleanup()
        assert future.result() == "already done"

    def test_cleanup_can_be_called_multiple_times(self) -> None:
        session = make_session()
        session.is_open = True
        session.cleanup()
        session.cleanup()  # Should not raise
        assert session.is_open is False


# ---------------------------------------------------------------------------
# Per-session state containers are isolated
# ---------------------------------------------------------------------------


class TestSessionIsolation:
    def test_sessions_have_independent_state(self) -> None:
        """Two sessions should not share state containers."""
        s1 = make_session()
        s2 = make_session()

        s1.server_rpcs["test"] = lambda: None
        s1.client_rpcs[1] = "com.test"
        s1.subscriptions[1] = "com.topic"

        assert s2.server_rpcs == {}
        assert s2.client_rpcs == {}
        assert s2.subscriptions == {}
