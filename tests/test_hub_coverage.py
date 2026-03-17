"""Additional hub tests to cover validation error paths and edge cases.

Targets uncovered lines in hub.py:
- Invalid message validation errors for CALL, REGISTER, UNREGISTER,
  SUBSCRIBE, UNSUBSCRIBE, PUBLISH, YIELD, ERROR
- Session close callback exceptions
- Message loop exception path (non-WebSocketDisconnect)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi_headless_wamp.hub import WampHub
from fastapi_headless_wamp.protocol import WampMessageType
from fastapi_headless_wamp.session import WampSession

# ---------------------------------------------------------------------------
# Mock WebSocket
# ---------------------------------------------------------------------------


class MockWebSocket:
    """Minimal mock WebSocket for hub tests."""

    def __init__(self, subprotocols: list[str] | None = None) -> None:
        self.sent_texts: list[str] = []
        self.sent_bytes: list[bytes] = []
        self._receive_text_queue: asyncio.Queue[str] = asyncio.Queue()
        self._closed = False
        self._close_code: int | None = None
        self._accepted = False
        self._accepted_subprotocol: str | None = None
        self.scope: dict[str, Any] = {
            "subprotocols": subprotocols or [],
        }

    async def accept(self, subprotocol: str | None = None) -> None:
        self._accepted = True
        self._accepted_subprotocol = subprotocol

    async def close(self, code: int = 1000) -> None:
        self._closed = True
        self._close_code = code

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


def make_hello() -> list[Any]:
    return [WampMessageType.HELLO, "realm1", {"roles": {}}]


def make_goodbye() -> list[Any]:
    return [WampMessageType.GOODBYE, {}, "wamp.close.normal"]


# ---------------------------------------------------------------------------
# Tests: Invalid message validation error paths
# ---------------------------------------------------------------------------


class TestInvalidCallMessage:
    """Hub should silently drop an invalid CALL message."""

    async def test_invalid_call_is_dropped(self) -> None:
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        # Invalid CALL: missing required fields (only type code, no request_id etc.)
        ws.enqueue_text(json.dumps([WampMessageType.CALL]))
        ws.enqueue_text(json.dumps(make_goodbye()))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Should still complete without error; WELCOME + GOODBYE sent
        sent = [json.loads(t) for t in ws.sent_texts]
        msg_types = [m[0] for m in sent]
        assert WampMessageType.WELCOME in msg_types
        assert WampMessageType.GOODBYE in msg_types


class TestInvalidRegisterMessage:
    """Hub should silently drop an invalid REGISTER message."""

    async def test_invalid_register_is_dropped(self) -> None:
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        # Invalid REGISTER: missing fields
        ws.enqueue_text(json.dumps([WampMessageType.REGISTER]))
        ws.enqueue_text(json.dumps(make_goodbye()))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        sent = [json.loads(t) for t in ws.sent_texts]
        msg_types = [m[0] for m in sent]
        assert WampMessageType.WELCOME in msg_types
        assert WampMessageType.GOODBYE in msg_types
        # No REGISTERED message should have been sent
        assert WampMessageType.REGISTERED not in msg_types


class TestInvalidUnregisterMessage:
    """Hub should silently drop an invalid UNREGISTER message."""

    async def test_invalid_unregister_is_dropped(self) -> None:
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        # Invalid UNREGISTER: missing fields
        ws.enqueue_text(json.dumps([WampMessageType.UNREGISTER]))
        ws.enqueue_text(json.dumps(make_goodbye()))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        sent = [json.loads(t) for t in ws.sent_texts]
        msg_types = [m[0] for m in sent]
        assert WampMessageType.WELCOME in msg_types
        assert WampMessageType.UNREGISTERED not in msg_types


class TestInvalidSubscribeMessage:
    """Hub should silently drop an invalid SUBSCRIBE message."""

    async def test_invalid_subscribe_is_dropped(self) -> None:
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        # Invalid SUBSCRIBE: missing fields
        ws.enqueue_text(json.dumps([WampMessageType.SUBSCRIBE]))
        ws.enqueue_text(json.dumps(make_goodbye()))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        sent = [json.loads(t) for t in ws.sent_texts]
        msg_types = [m[0] for m in sent]
        assert WampMessageType.WELCOME in msg_types
        assert WampMessageType.SUBSCRIBED not in msg_types


class TestInvalidUnsubscribeMessage:
    """Hub should silently drop an invalid UNSUBSCRIBE message."""

    async def test_invalid_unsubscribe_is_dropped(self) -> None:
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        # Invalid UNSUBSCRIBE: missing fields
        ws.enqueue_text(json.dumps([WampMessageType.UNSUBSCRIBE]))
        ws.enqueue_text(json.dumps(make_goodbye()))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        sent = [json.loads(t) for t in ws.sent_texts]
        msg_types = [m[0] for m in sent]
        assert WampMessageType.WELCOME in msg_types
        assert WampMessageType.UNSUBSCRIBED not in msg_types


class TestInvalidPublishMessage:
    """Hub should silently drop an invalid PUBLISH message."""

    async def test_invalid_publish_is_dropped(self) -> None:
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        # Invalid PUBLISH: missing fields
        ws.enqueue_text(json.dumps([WampMessageType.PUBLISH]))
        ws.enqueue_text(json.dumps(make_goodbye()))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        sent = [json.loads(t) for t in ws.sent_texts]
        msg_types = [m[0] for m in sent]
        assert WampMessageType.WELCOME in msg_types
        assert WampMessageType.PUBLISHED not in msg_types


class TestInvalidYieldMessage:
    """Hub should silently drop an invalid YIELD message."""

    async def test_invalid_yield_is_dropped(self) -> None:
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        # Invalid YIELD: missing fields
        ws.enqueue_text(json.dumps([WampMessageType.YIELD]))
        ws.enqueue_text(json.dumps(make_goodbye()))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        sent = [json.loads(t) for t in ws.sent_texts]
        msg_types = [m[0] for m in sent]
        assert WampMessageType.WELCOME in msg_types


class TestInvalidErrorMessage:
    """Hub should silently drop an invalid ERROR message."""

    async def test_invalid_error_is_dropped(self) -> None:
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        # Invalid ERROR: missing fields
        ws.enqueue_text(json.dumps([WampMessageType.ERROR]))
        ws.enqueue_text(json.dumps(make_goodbye()))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        sent = [json.loads(t) for t in ws.sent_texts]
        msg_types = [m[0] for m in sent]
        assert WampMessageType.WELCOME in msg_types


# ---------------------------------------------------------------------------
# Tests: Session close callback exception handling
# ---------------------------------------------------------------------------


class TestSessionCloseCallbackError:
    """on_session_close callback exception should be caught and logged."""

    async def test_close_callback_exception_is_caught(self) -> None:
        hub = WampHub(realm="realm1")
        closed_sessions: list[int] = []

        @hub.on_session_close
        async def bad_close_callback(session: WampSession) -> None:
            msg = "intentional error"
            raise RuntimeError(msg)

        @hub.on_session_close
        async def good_close_callback(session: WampSession) -> None:
            closed_sessions.append(session.session_id)

        ws = MockWebSocket(subprotocols=["wamp.2.json"])
        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_goodbye()))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Good callback should still run even though bad one raised
        assert len(closed_sessions) == 1


# ---------------------------------------------------------------------------
# Tests: Message loop exception path (non-WebSocketDisconnect)
# ---------------------------------------------------------------------------


class TestMessageLoopException:
    """Hub catches non-WebSocketDisconnect exceptions during message loop."""

    async def test_generic_exception_in_loop_is_caught(self) -> None:
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))

        async def crashing_loop(session: WampSession) -> None:
            msg = "something broke"
            raise RuntimeError(msg)

        hub._message_loop = crashing_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Session should still be cleaned up
        assert hub.session_count == 0


# ---------------------------------------------------------------------------
# Tests: Empty message in dispatch loop
# ---------------------------------------------------------------------------


class TestEmptyMessageInLoop:
    """Empty messages in the dispatch loop should be skipped."""

    async def test_empty_message_skipped(self) -> None:
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        # Send an empty list (should be skipped)
        ws.enqueue_text(json.dumps([]))
        ws.enqueue_text(json.dumps(make_goodbye()))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]
        assert hub.session_count == 0
