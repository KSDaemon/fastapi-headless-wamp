"""Integration tests for FastAPI integration patterns (US-020).

Tests both the manual ``handle_websocket`` approach and the
``get_router`` auto-mount approach.  Verifies full handshake, RPC
calls, and graceful disconnect through a real FastAPI test client.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, WebSocket as FastAPIWebSocket
from starlette.testclient import TestClient

from fastapi_headless_wamp.hub import WampHub
from fastapi_headless_wamp.protocol import (
    WAMP_ERROR_GOODBYE_AND_OUT,
    WAMP_ERROR_NO_SUCH_PROCEDURE,
    WampMessageType,
)
from fastapi_headless_wamp.session import WampSession


# ---------------------------------------------------------------------------
# Helper: perform WAMP handshake over a test WS
# ---------------------------------------------------------------------------


def do_handshake(ws: Any, realm: str = "realm1") -> int:
    """Send HELLO, receive WELCOME, return session ID."""
    hello: list[Any] = [
        WampMessageType.HELLO,
        realm,
        {"roles": {"caller": {}, "callee": {}}},
    ]
    ws.send_json(hello)
    welcome: list[Any] = ws.receive_json()
    assert welcome[0] == WampMessageType.WELCOME
    assert isinstance(welcome[1], int) and welcome[1] > 0
    return int(welcome[1])


def do_goodbye(ws: Any) -> None:
    """Send GOODBYE, receive GOODBYE reply."""
    goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.close_realm"]
    ws.send_json(goodbye)
    reply: list[Any] = ws.receive_json()
    assert reply[0] == WampMessageType.GOODBYE
    assert reply[2] == WAMP_ERROR_GOODBYE_AND_OUT


# ===========================================================================
# Pattern 1: Manual handle_websocket
# ===========================================================================


class TestManualHandleWebsocket:
    """WampHub.handle_websocket(websocket) used in a manual @app.websocket handler."""

    def _make_app(self, hub: WampHub) -> FastAPI:
        app = FastAPI()

        @app.websocket("/ws")
        async def wamp_endpoint(websocket: FastAPIWebSocket) -> None:
            await hub.handle_websocket(websocket)

        return app

    def test_connect_handshake_disconnect(self) -> None:
        """Full lifecycle: connect → HELLO → WELCOME → GOODBYE → disconnect."""
        hub = WampHub(realm="realm1")
        app = self._make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                session_id = do_handshake(ws)
                assert session_id > 0
                do_goodbye(ws)

        assert hub.session_count == 0

    def test_handle_websocket_blocks_until_disconnect(self) -> None:
        """handle_websocket blocks while the session is active and the
        session is accessible via wamp.sessions during the connection."""
        hub = WampHub(realm="realm1")
        app = self._make_app(hub)

        observed_session_ids: list[int] = []

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            # Session should be in hub.sessions while connected
            observed_session_ids.append(session.session_id)
            assert session.session_id in hub.sessions

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)
                do_goodbye(ws)

        assert len(observed_session_ids) == 1
        assert hub.session_count == 0

    def test_call_rpc_via_manual_handler(self) -> None:
        """Register an RPC, call it via CALL, receive RESULT."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.add")
        async def add(a: int, b: int) -> int:
            return a + b

        app = self._make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                # Call the RPC
                call_msg: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {},
                    "com.example.add",
                    [3, 4],
                ]
                ws.send_json(call_msg)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 1  # request_id
                assert result[3] == [7]  # [return_value]

                do_goodbye(ws)

        assert hub.session_count == 0

    def test_lifecycle_callbacks_fire(self) -> None:
        """on_session_open and on_session_close fire correctly."""
        hub = WampHub(realm="realm1")
        events: list[str] = []

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            events.append("open")

        @hub.on_session_close
        async def on_close(session: WampSession) -> None:
            events.append("close")

        app = self._make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)
                do_goodbye(ws)

        assert events == ["open", "close"]


# ===========================================================================
# Pattern 2: Auto-mount via get_router
# ===========================================================================


class TestGetRouter:
    """WampHub.get_router(path) returns a pre-configured APIRouter."""

    def test_default_path(self) -> None:
        """get_router() with default path='/ws' mounts at /ws."""
        hub = WampHub(realm="realm1")
        app = FastAPI()
        app.include_router(hub.get_router())

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                session_id = do_handshake(ws)
                assert session_id > 0
                do_goodbye(ws)

        assert hub.session_count == 0

    def test_custom_path(self) -> None:
        """get_router(path='/custom') mounts at the specified path."""
        hub = WampHub(realm="realm1")
        app = FastAPI()
        app.include_router(hub.get_router(path="/custom"))

        with TestClient(app) as client:
            with client.websocket_connect(
                "/custom", subprotocols=["wamp.2.json"]
            ) as ws:
                session_id = do_handshake(ws)
                assert session_id > 0
                do_goodbye(ws)

        assert hub.session_count == 0

    def test_call_rpc_via_router(self) -> None:
        """Register an RPC and call it through the router-mounted endpoint."""
        hub = WampHub(realm="realm1")

        @hub.register("com.math.multiply")
        async def multiply(x: int, y: int) -> int:
            return x * y

        app = FastAPI()
        app.include_router(hub.get_router(path="/wamp"))

        with TestClient(app) as client:
            with client.websocket_connect("/wamp", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                call_msg: list[Any] = [
                    WampMessageType.CALL,
                    42,
                    {},
                    "com.math.multiply",
                    [6, 7],
                ]
                ws.send_json(call_msg)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 42
                assert result[3] == [42]

                do_goodbye(ws)

    def test_lifecycle_callbacks_via_router(self) -> None:
        """Lifecycle callbacks work through the auto-mounted router."""
        hub = WampHub(realm="realm1")
        events: list[str] = []

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            events.append("open")

        @hub.on_session_close
        async def on_close(session: WampSession) -> None:
            events.append("close")

        app = FastAPI()
        app.include_router(hub.get_router())

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)
                do_goodbye(ws)

        assert events == ["open", "close"]

    def test_no_such_procedure_error(self) -> None:
        """CALL to unregistered procedure returns ERROR via router endpoint."""
        hub = WampHub(realm="realm1")
        app = FastAPI()
        app.include_router(hub.get_router())

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                call_msg: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {},
                    "com.nonexistent.proc",
                ]
                ws.send_json(call_msg)

                error: list[Any] = ws.receive_json()
                assert error[0] == WampMessageType.ERROR
                assert error[1] == WampMessageType.CALL
                assert error[4] == WAMP_ERROR_NO_SUCH_PROCEDURE

                do_goodbye(ws)


# ===========================================================================
# Both patterns: feature parity
# ===========================================================================


class TestBothPatterns:
    """Verify that both manual and router patterns behave identically."""

    @staticmethod
    def _manual_app(hub: WampHub) -> FastAPI:
        app = FastAPI()

        @app.websocket("/ws")
        async def wamp_endpoint(websocket: FastAPIWebSocket) -> None:
            await hub.handle_websocket(websocket)

        return app

    @staticmethod
    def _router_app(hub: WampHub) -> FastAPI:
        app = FastAPI()
        app.include_router(hub.get_router())
        return app

    @pytest.mark.parametrize("pattern", ["manual", "router"])
    def test_handshake_and_rpc(self, pattern: str) -> None:
        """Connect, handshake, call RPC, disconnect — same behavior."""
        hub = WampHub(realm="realm1")

        @hub.register("com.test.echo")
        async def echo(msg: str) -> str:
            return msg

        if pattern == "manual":
            app = self._manual_app(hub)
        else:
            app = self._router_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                session_id = do_handshake(ws)
                assert session_id > 0

                # CALL echo
                call_msg: list[Any] = [
                    WampMessageType.CALL,
                    10,
                    {},
                    "com.test.echo",
                    ["hello"],
                ]
                ws.send_json(call_msg)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[3] == ["hello"]

                do_goodbye(ws)

        assert hub.session_count == 0

    @pytest.mark.parametrize("pattern", ["manual", "router"])
    def test_subscribe_and_publish(self, pattern: str) -> None:
        """Subscribe to a topic, verify SUBSCRIBED response."""
        hub = WampHub(realm="realm1")

        if pattern == "manual":
            app = self._manual_app(hub)
        else:
            app = self._router_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                # Subscribe
                sub_msg: list[Any] = [
                    WampMessageType.SUBSCRIBE,
                    1,
                    {},
                    "com.test.topic",
                ]
                ws.send_json(sub_msg)

                subscribed: list[Any] = ws.receive_json()
                assert subscribed[0] == WampMessageType.SUBSCRIBED
                assert subscribed[1] == 1  # request_id
                assert isinstance(subscribed[2], int)  # subscription_id

                do_goodbye(ws)

        assert hub.session_count == 0

    @pytest.mark.parametrize("pattern", ["manual", "router"])
    def test_abort_on_bad_first_message(self, pattern: str) -> None:
        """ABORT sent when first message is not HELLO."""
        hub = WampHub(realm="realm1")

        if pattern == "manual":
            app = self._manual_app(hub)
        else:
            app = self._router_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                # Send CALL instead of HELLO
                bad: list[Any] = [WampMessageType.CALL, 1, {}, "com.foo"]
                ws.send_json(bad)

                abort: list[Any] = ws.receive_json()
                assert abort[0] == WampMessageType.ABORT

        assert hub.session_count == 0
