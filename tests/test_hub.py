"""Tests for WampHub core and session tracking (US-007).

Covers:
- WampHub.sessions and session_count properties
- Sessions added on WELCOME, removed on disconnect/GOODBYE
- @wamp.on_session_open and @wamp.on_session_close lifecycle callbacks
- Message dispatch loop routes by message type code
- asyncio-safe hub state
- Integration test: FastAPI test client connects, handshake, disconnect
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI
from fastapi import WebSocket as FastAPIWebSocket
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fastapi_headless_wamp.hub import WampHub
from fastapi_headless_wamp.protocol import (
    WAMP_ERROR_GOODBYE_AND_OUT,
    WampMessageType,
)
from fastapi_headless_wamp.session import WampSession

# ---------------------------------------------------------------------------
# Mock WebSocket (reusable helper identical to test_handshake)
# ---------------------------------------------------------------------------


class MockWebSocket:
    """Mock that simulates Starlette's WebSocket."""

    def __init__(self, subprotocols: list[str] | None = None) -> None:
        self.sent_texts: list[str] = []
        self.sent_bytes: list[bytes] = []
        self._receive_text_queue: asyncio.Queue[str] = asyncio.Queue()
        self._receive_bytes_queue: asyncio.Queue[bytes] = asyncio.Queue()
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


def parse_sent(ws: MockWebSocket, index: int = 0) -> list[Any]:
    """Parse JSON message at *index*."""
    result: list[Any] = json.loads(ws.sent_texts[index])
    return result


# ---------------------------------------------------------------------------
# Hub basic properties
# ---------------------------------------------------------------------------


class TestHubProperties:
    """WampHub.sessions and session_count."""

    def test_initial_state(self) -> None:
        hub = WampHub(realm="test")
        assert hub.realm == "test"
        assert hub.sessions == {}
        assert hub.session_count == 0

    def test_custom_realm(self) -> None:
        hub = WampHub(realm="my_realm")
        assert hub.realm == "my_realm"


# ---------------------------------------------------------------------------
# Session tracking (add on WELCOME, remove on disconnect/GOODBYE)
# ---------------------------------------------------------------------------


class TestSessionTracking:
    """Sessions added after WELCOME, removed on disconnect/GOODBYE."""

    async def test_session_added_on_welcome(self) -> None:
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        ws.enqueue_text(json.dumps(hello))

        # Use an event to pause inside the message loop
        entered_loop = asyncio.Event()
        release_loop = asyncio.Event()

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            entered_loop.set()
            await release_loop.wait()
            goodbye: list[Any] = [
                WampMessageType.GOODBYE,
                {},
                "wamp.close.close_realm",
            ]
            ws.enqueue_text(json.dumps(goodbye))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        task = asyncio.create_task(hub.handle_websocket(ws))  # type: ignore[arg-type]
        await entered_loop.wait()

        # Session should be tracked
        assert hub.session_count == 1
        session = next(iter(hub.sessions.values()))
        assert session.is_open is True

        release_loop.set()
        await task

        # After GOODBYE + cleanup
        assert hub.session_count == 0

    async def test_session_removed_on_goodbye(self) -> None:
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.close_realm"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]
        assert hub.session_count == 0

    async def test_session_removed_on_disconnect(self) -> None:
        """Session removed from hub when WebSocket disconnects unexpectedly."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        ws.enqueue_text(json.dumps(hello))

        # Don't enqueue a GOODBYE; the async iterator will raise
        # StopAsyncIteration when the queue is empty and the task ends.
        # We need to simulate a disconnect by putting a bad message or
        # closing: let's use a hooked loop that raises WebSocketDisconnect.

        async def crash_loop(session: WampSession) -> None:
            raise WebSocketDisconnect

        hub._message_loop = crash_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]
        assert hub.session_count == 0


# ---------------------------------------------------------------------------
# Lifecycle callbacks
# ---------------------------------------------------------------------------


class TestLifecycleCallbacks:
    """@wamp.on_session_open and @wamp.on_session_close decorators."""

    async def test_on_session_open_fires(self) -> None:
        hub = WampHub(realm="realm1")
        opened_sessions: list[int] = []

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            opened_sessions.append(session.session_id)

        ws = MockWebSocket(subprotocols=["wamp.2.json"])
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.close_realm"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert len(opened_sessions) == 1
        assert opened_sessions[0] > 0

    async def test_on_session_close_fires(self) -> None:
        hub = WampHub(realm="realm1")
        closed_sessions: list[int] = []

        @hub.on_session_close
        async def on_close(session: WampSession) -> None:
            closed_sessions.append(session.session_id)

        ws = MockWebSocket(subprotocols=["wamp.2.json"])
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.close_realm"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert len(closed_sessions) == 1
        assert closed_sessions[0] > 0

    async def test_both_callbacks_fire_in_order(self) -> None:
        hub = WampHub(realm="realm1")
        events: list[str] = []

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            events.append(f"open:{session.session_id}")

        @hub.on_session_close
        async def on_close(session: WampSession) -> None:
            events.append(f"close:{session.session_id}")

        ws = MockWebSocket(subprotocols=["wamp.2.json"])
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.close_realm"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert len(events) == 2
        assert events[0].startswith("open:")
        assert events[1].startswith("close:")
        # Same session ID
        sid_open = events[0].split(":")[1]
        sid_close = events[1].split(":")[1]
        assert sid_open == sid_close

    async def test_multiple_open_callbacks(self) -> None:
        hub = WampHub(realm="realm1")
        cb1_called = False
        cb2_called = False

        @hub.on_session_open
        async def on_open_1(session: WampSession) -> None:
            nonlocal cb1_called
            cb1_called = True

        @hub.on_session_open
        async def on_open_2(session: WampSession) -> None:
            nonlocal cb2_called
            cb2_called = True

        ws = MockWebSocket(subprotocols=["wamp.2.json"])
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.close_realm"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert cb1_called is True
        assert cb2_called is True

    async def test_callback_error_does_not_crash_hub(self) -> None:
        hub = WampHub(realm="realm1")
        after_error_called = False

        @hub.on_session_open
        async def bad_callback(session: WampSession) -> None:
            raise RuntimeError("boom")

        @hub.on_session_open
        async def good_callback(session: WampSession) -> None:
            nonlocal after_error_called
            after_error_called = True

        ws = MockWebSocket(subprotocols=["wamp.2.json"])
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.close_realm"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # The second callback should still fire despite the first raising
        assert after_error_called is True
        assert hub.session_count == 0

    async def test_close_callback_fires_on_disconnect(self) -> None:
        """on_session_close fires even on unexpected disconnects."""

        hub = WampHub(realm="realm1")
        closed_sessions: list[int] = []

        @hub.on_session_close
        async def on_close(session: WampSession) -> None:
            closed_sessions.append(session.session_id)

        ws = MockWebSocket(subprotocols=["wamp.2.json"])
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        ws.enqueue_text(json.dumps(hello))

        async def crash_loop(session: WampSession) -> None:
            raise WebSocketDisconnect

        hub._message_loop = crash_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert len(closed_sessions) == 1

    async def test_close_callback_receives_session_before_cleanup(self) -> None:
        """on_session_close sees the session still open (before cleanup clears state)."""
        hub = WampHub(realm="realm1")
        session_was_open: list[bool] = []
        had_session_id: list[bool] = []

        @hub.on_session_close
        async def on_close(session: WampSession) -> None:
            # Session should still be "open" at this point; cleanup happens AFTER
            # the callbacks
            session_was_open.append(session.session_id > 0)
            had_session_id.append(session.session_id in hub.sessions)

        ws = MockWebSocket(subprotocols=["wamp.2.json"])
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.close_realm"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert session_was_open == [True]
        # Session is still in hub.sessions during the callback
        assert had_session_id == [True]

    async def test_no_callbacks_on_handshake_failure(self) -> None:
        """Lifecycle callbacks should NOT fire if handshake fails."""
        hub = WampHub(realm="realm1")
        opened: list[int] = []
        closed: list[int] = []

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            opened.append(session.session_id)

        @hub.on_session_close
        async def on_close(session: WampSession) -> None:
            closed.append(session.session_id)

        ws = MockWebSocket(subprotocols=["wamp.2.json"])
        # Send a CALL instead of HELLO -> handshake fails
        bad_msg: list[Any] = [WampMessageType.CALL, 1, {}, "com.foo"]
        ws.enqueue_text(json.dumps(bad_msg))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert opened == []
        assert closed == []


# ---------------------------------------------------------------------------
# Message dispatch routing
# ---------------------------------------------------------------------------


class TestMessageDispatch:
    """Message dispatch loop routes by message type code."""

    async def test_goodbye_breaks_loop(self) -> None:
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # WELCOME + GOODBYE reply
        assert len(ws.sent_texts) == 2
        goodbye_reply = json.loads(ws.sent_texts[1])
        assert goodbye_reply[0] == WampMessageType.GOODBYE
        assert goodbye_reply[2] == WAMP_ERROR_GOODBYE_AND_OUT

    async def test_unknown_message_type_skipped(self) -> None:
        """Unknown message types are logged and skipped, not crashing."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        # Unknown type code 999
        unknown: list[Any] = [999, "some", "data"]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(unknown))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Should still complete: WELCOME + GOODBYE reply
        assert len(ws.sent_texts) == 2

    async def test_unhandled_message_type_logged_not_crashed(self) -> None:
        """Known but unhandled message types (e.g. PUBLISHED) are skipped for now."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        # PUBLISHED message type is known but has no handler yet
        published: list[Any] = [WampMessageType.PUBLISHED, 1, 1]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(published))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Should still complete gracefully: WELCOME + GOODBYE reply
        assert len(ws.sent_texts) == 2


# ---------------------------------------------------------------------------
# FastAPI integration test (TestClient + WebSocket)
# ---------------------------------------------------------------------------


class TestFastAPIIntegration:
    """Integration test using FastAPI test client (httpx)."""

    def _make_app(self, hub: WampHub) -> FastAPI:
        """Create a FastAPI app with a /ws endpoint."""
        app = FastAPI()

        @app.websocket("/ws")
        async def wamp_endpoint(websocket: FastAPIWebSocket) -> None:
            await hub.handle_websocket(websocket)

        return app

    def test_connect_handshake_disconnect(self) -> None:
        """FastAPI test client: connect, HELLO, WELCOME, GOODBYE, disconnect."""
        hub = WampHub(realm="realm1")
        app = self._make_app(hub)

        open_ids: list[int] = []
        close_ids: list[int] = []

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            open_ids.append(session.session_id)

        @hub.on_session_close
        async def on_close(session: WampSession) -> None:
            close_ids.append(session.session_id)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                # Send HELLO
                hello: list[Any] = [
                    WampMessageType.HELLO,
                    "realm1",
                    {"roles": {"caller": {}, "callee": {}}},
                ]
                ws.send_json(hello)

                # Receive WELCOME
                welcome: list[Any] = ws.receive_json()
                assert welcome[0] == WampMessageType.WELCOME
                assert isinstance(welcome[1], int)
                assert welcome[1] > 0
                session_id = welcome[1]

                # Verify roles in WELCOME
                details = welcome[2]
                assert "dealer" in details["roles"]
                assert "broker" in details["roles"]

                # Send GOODBYE
                goodbye: list[Any] = [
                    WampMessageType.GOODBYE,
                    {},
                    "wamp.close.close_realm",
                ]
                ws.send_json(goodbye)

                # Receive GOODBYE reply
                goodbye_reply: list[Any] = ws.receive_json()
                assert goodbye_reply[0] == WampMessageType.GOODBYE
                assert goodbye_reply[2] == WAMP_ERROR_GOODBYE_AND_OUT

        # After disconnect, verify callbacks fired
        assert len(open_ids) == 1
        assert open_ids[0] == session_id
        assert len(close_ids) == 1
        assert close_ids[0] == session_id

        # Hub should have no active sessions
        assert hub.session_count == 0

    def test_multiple_connections_isolated(self) -> None:
        """Two sequential FastAPI WebSocket connections get unique session IDs."""
        hub = WampHub(realm="realm1")
        app = self._make_app(hub)
        session_ids: list[int] = []

        with TestClient(app) as client:
            for _ in range(2):
                with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                    hello: list[Any] = [
                        WampMessageType.HELLO,
                        "realm1",
                        {"roles": {}},
                    ]
                    ws.send_json(hello)
                    welcome: list[Any] = ws.receive_json()
                    session_ids.append(welcome[1])

                    goodbye: list[Any] = [
                        WampMessageType.GOODBYE,
                        {},
                        "wamp.close.normal",
                    ]
                    ws.send_json(goodbye)
                    ws.receive_json()  # GOODBYE reply

        assert len(session_ids) == 2
        assert session_ids[0] != session_ids[1]
        assert hub.session_count == 0

    def test_handshake_abort_fastapi(self) -> None:
        """FastAPI test: ABORT sent when first message is not HELLO."""
        hub = WampHub(realm="realm1")
        app = self._make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                # Send CALL instead of HELLO
                call: list[Any] = [WampMessageType.CALL, 1, {}, "com.add"]
                ws.send_json(call)

                # Receive ABORT
                abort: list[Any] = ws.receive_json()
                assert abort[0] == WampMessageType.ABORT

        assert hub.session_count == 0

    def test_lifecycle_callbacks_with_fastapi(self) -> None:
        """on_session_open / on_session_close fire with real FastAPI WebSocket."""
        hub = WampHub(realm="realm1")
        app = self._make_app(hub)

        events: list[str] = []

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            events.append("open")

        @hub.on_session_close
        async def on_close(session: WampSession) -> None:
            events.append("close")

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                hello: list[Any] = [
                    WampMessageType.HELLO,
                    "realm1",
                    {"roles": {}},
                ]
                ws.send_json(hello)
                ws.receive_json()  # WELCOME

                goodbye: list[Any] = [
                    WampMessageType.GOODBYE,
                    {},
                    "wamp.close.normal",
                ]
                ws.send_json(goodbye)
                ws.receive_json()  # GOODBYE reply

        assert events == ["open", "close"]
