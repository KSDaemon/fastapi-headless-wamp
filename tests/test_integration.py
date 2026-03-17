"""Integration tests for FastAPI WAMP integration (US-020 + US-021).

Tests both the manual ``handle_websocket`` approach and the
``get_router`` auto-mount approach.  Verifies full handshake, RPC
calls, and graceful disconnect through a real FastAPI test client.

US-021 adds comprehensive end-to-end integration tests simulating
wampy.js message sequences for the full feature surface:
- Full session lifecycle (connect, handshake, RPC, PubSub, GOODBYE)
- Bidirectional RPC
- Progressive call results
- Call cancellation
- Multiple concurrent connections
- Error paths
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi import WebSocket as FastAPIWebSocket
from starlette.testclient import TestClient

from fastapi_headless_wamp.hub import WampHub
from fastapi_headless_wamp.protocol import (
    WAMP_ERROR_CANCELED,
    WAMP_ERROR_GOODBYE_AND_OUT,
    WAMP_ERROR_NO_SUCH_PROCEDURE,
    WAMP_ERROR_NO_SUCH_SUBSCRIPTION,
    WAMP_ERROR_RUNTIME_ERROR,
    WampMessageType,
)
from fastapi_headless_wamp.service import WampService, rpc, subscribe
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
    assert isinstance(welcome[1], int)
    assert welcome[1] > 0
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
            with client.websocket_connect("/custom", subprotocols=["wamp.2.json"]) as ws:
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

        if pattern == "manual":  # noqa: SIM108
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

        if pattern == "manual":  # noqa: SIM108
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

        if pattern == "manual":  # noqa: SIM108
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


# ===========================================================================
# US-021: Comprehensive integration test suite
# ===========================================================================


def _make_app(hub: WampHub) -> FastAPI:
    """Create a FastAPI app with a manual WAMP WebSocket endpoint."""
    app = FastAPI()

    @app.websocket("/ws")
    async def wamp_endpoint(websocket: FastAPIWebSocket) -> None:
        await hub.handle_websocket(websocket)

    return app


class TestFullSessionLifecycle:
    """Full session lifecycle: connect, handshake, RPC call, PubSub
    subscribe+event, GOODBYE."""

    def test_connect_rpc_pubsub_goodbye(self) -> None:
        """Complete lifecycle: handshake -> RPC -> subscribe -> event ->
        unsubscribe -> GOODBYE."""
        hub = WampHub(realm="realm1")
        events_received: list[dict[str, Any]] = []

        @hub.register("com.example.greet")
        async def greet(name: str) -> str:
            return f"Hello, {name}!"

        @hub.subscribe("com.example.notifications")
        async def on_notification(message: str, _session: WampSession | None = None) -> None:
            events_received.append({"message": message})

        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                # 1. Handshake
                session_id = do_handshake(ws)
                assert session_id > 0

                # 2. Call RPC
                call_msg: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {},
                    "com.example.greet",
                    ["World"],
                ]
                ws.send_json(call_msg)
                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 1
                assert result[3] == ["Hello, World!"]

                # 3. Subscribe to a topic
                sub_msg: list[Any] = [
                    WampMessageType.SUBSCRIBE,
                    2,
                    {},
                    "com.example.chat",
                ]
                ws.send_json(sub_msg)
                subscribed: list[Any] = ws.receive_json()
                assert subscribed[0] == WampMessageType.SUBSCRIBED
                assert subscribed[1] == 2
                subscription_id = subscribed[2]
                assert isinstance(subscription_id, int)

                # 4. Client publishes to a server-subscribed topic (with ack)
                pub_msg: list[Any] = [
                    WampMessageType.PUBLISH,
                    3,
                    {"acknowledge": True},
                    "com.example.notifications",
                    ["test message"],
                ]
                ws.send_json(pub_msg)
                published: list[Any] = ws.receive_json()
                assert published[0] == WampMessageType.PUBLISHED
                assert published[1] == 3

                # Server handler should have been invoked
                assert len(events_received) == 1
                assert events_received[0]["message"] == "test message"

                # 5. Unsubscribe
                unsub_msg: list[Any] = [
                    WampMessageType.UNSUBSCRIBE,
                    4,
                    subscription_id,
                ]
                ws.send_json(unsub_msg)
                unsubscribed: list[Any] = ws.receive_json()
                assert unsubscribed[0] == WampMessageType.UNSUBSCRIBED
                assert unsubscribed[1] == 4

                # 6. GOODBYE
                do_goodbye(ws)

        assert hub.session_count == 0

    def test_welcome_message_contains_roles(self) -> None:
        """WELCOME message includes dealer and broker roles with features."""
        hub = WampHub(realm="realm1")
        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                hello: list[Any] = [
                    WampMessageType.HELLO,
                    "realm1",
                    {"roles": {"caller": {}, "callee": {}}},
                ]
                ws.send_json(hello)
                welcome: list[Any] = ws.receive_json()

                assert welcome[0] == WampMessageType.WELCOME
                details = welcome[2]
                assert "roles" in details
                assert "dealer" in details["roles"]
                assert "broker" in details["roles"]

                dealer_features = details["roles"]["dealer"]["features"]
                assert dealer_features["progressive_call_results"] is True
                assert dealer_features["call_canceling"] is True
                assert dealer_features["caller_identification"] is True
                assert dealer_features["call_timeout"] is True

                broker_features = details["roles"]["broker"]["features"]
                assert broker_features["publisher_identification"] is True
                assert broker_features["publisher_exclusion"] is True
                assert broker_features["subscriber_blackwhite_listing"] is True

                do_goodbye(ws)


class TestBidirectionalRPC:
    """Server registers handler, client calls it; client registers handler,
    server calls it."""

    def test_server_to_client_rpc_call(self) -> None:
        """Server calls a client-registered RPC via on_session_open."""
        hub = WampHub(realm="realm1")
        call_results: list[Any] = []
        call_errors: list[str] = []

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            # Wait for client to register its procedure
            async def do_call() -> None:
                await asyncio.sleep(0.1)
                try:
                    result = await session.call("com.client.compute", args=[10, 20], timeout=5.0)
                    call_results.append(result)
                except Exception as e:
                    call_errors.append(str(e))

            asyncio.create_task(do_call())

        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                # Client registers a procedure
                register_msg: list[Any] = [
                    WampMessageType.REGISTER,
                    1,
                    {},
                    "com.client.compute",
                ]
                ws.send_json(register_msg)
                registered: list[Any] = ws.receive_json()
                assert registered[0] == WampMessageType.REGISTERED
                registration_id = registered[2]

                # Wait for server to send INVOCATION
                invocation: list[Any] = ws.receive_json()
                assert invocation[0] == WampMessageType.INVOCATION
                request_id = invocation[1]
                assert invocation[2] == registration_id
                assert invocation[4] == [10, 20]

                # Client sends YIELD with result
                yield_msg: list[Any] = [
                    WampMessageType.YIELD,
                    request_id,
                    {},
                    [30],
                ]
                ws.send_json(yield_msg)

                # Small delay to let the server process the YIELD
                time.sleep(0.1)

                do_goodbye(ws)

        assert len(call_errors) == 0, f"Server call errors: {call_errors}"
        assert call_results == [30]
        assert hub.session_count == 0

    def test_client_calls_server_rpc(self) -> None:
        """Client calls a server-registered RPC with kwargs."""
        hub = WampHub(realm="realm1")

        @hub.register("com.math.power")
        async def power(base: int, exp: int) -> int:
            return base**exp

        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                call_msg: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {},
                    "com.math.power",
                    [],
                    {"base": 2, "exp": 10},
                ]
                ws.send_json(call_msg)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[3] == [1024]

                do_goodbye(ws)

    def test_bidirectional_in_same_session(self) -> None:
        """Both server->client and client->server RPC in one session."""
        hub = WampHub(realm="realm1")
        server_call_results: list[Any] = []

        @hub.register("com.server.add")
        async def add(a: int, b: int) -> int:
            return a + b

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            async def do_call() -> None:
                await asyncio.sleep(0.15)
                try:
                    result = await session.call("com.client.subtract", args=[50, 20], timeout=5.0)
                    server_call_results.append(result)
                except Exception as e:
                    server_call_results.append(f"error: {e}")

            asyncio.create_task(do_call())

        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                # 1. Client calls server RPC
                call_msg: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {},
                    "com.server.add",
                    [3, 7],
                ]
                ws.send_json(call_msg)
                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[3] == [10]

                # 2. Client registers a procedure for server to call
                register_msg: list[Any] = [
                    WampMessageType.REGISTER,
                    2,
                    {},
                    "com.client.subtract",
                ]
                ws.send_json(register_msg)
                registered: list[Any] = ws.receive_json()
                assert registered[0] == WampMessageType.REGISTERED
                reg_id = registered[2]

                # 3. Wait for INVOCATION from server
                invocation: list[Any] = ws.receive_json()
                assert invocation[0] == WampMessageType.INVOCATION
                inv_request_id = invocation[1]
                assert invocation[2] == reg_id
                assert invocation[4] == [50, 20]

                # 4. Client yields result
                yield_msg: list[Any] = [
                    WampMessageType.YIELD,
                    inv_request_id,
                    {},
                    [30],
                ]
                ws.send_json(yield_msg)

                time.sleep(0.1)
                do_goodbye(ws)

        assert server_call_results == [30]
        assert hub.session_count == 0


class TestProgressiveResultsE2E:
    """Progressive call results flow end-to-end via FastAPI test client."""

    def test_progressive_results_from_server(self) -> None:
        """Client calls server RPC with receive_progress=true, gets
        progressive results followed by a final result."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.stream")
        async def stream_data(count: int, _progress: Any | None = None) -> str:
            if _progress is not None:
                for i in range(count):
                    await _progress(f"chunk-{i}")
            return "done"

        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                # CALL with receive_progress
                call_msg: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {"receive_progress": True},
                    "com.example.stream",
                    [3],
                ]
                ws.send_json(call_msg)

                # Receive 3 progressive results
                progress_results: list[str] = []
                for _ in range(3):
                    msg: list[Any] = ws.receive_json()
                    assert msg[0] == WampMessageType.RESULT
                    assert msg[1] == 1  # request_id
                    assert msg[2].get("progress") is True
                    progress_results.append(msg[3][0])

                assert progress_results == ["chunk-0", "chunk-1", "chunk-2"]

                # Receive final result
                final: list[Any] = ws.receive_json()
                assert final[0] == WampMessageType.RESULT
                assert final[1] == 1
                assert final[2] == {}  # no progress flag
                assert final[3] == ["done"]

                do_goodbye(ws)

    def test_progressive_results_server_to_client(self) -> None:
        """Server calls client RPC with receive_progress, client sends
        progressive YIELDs."""
        hub = WampHub(realm="realm1")
        progress_values: list[Any] = []
        final_result: list[Any] = []

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            async def do_call() -> None:
                await asyncio.sleep(0.1)
                try:

                    async def on_progress(value: Any) -> None:
                        progress_values.append(value)

                    result = await session.call(
                        "com.client.stream",
                        receive_progress=True,
                        on_progress=on_progress,
                        timeout=5.0,
                    )
                    final_result.append(result)
                except Exception as e:
                    final_result.append(f"error: {e}")

            asyncio.create_task(do_call())

        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                # Client registers procedure
                register_msg: list[Any] = [
                    WampMessageType.REGISTER,
                    1,
                    {},
                    "com.client.stream",
                ]
                ws.send_json(register_msg)
                registered: list[Any] = ws.receive_json()
                assert registered[0] == WampMessageType.REGISTERED
                reg_id = registered[2]

                # Wait for INVOCATION
                invocation: list[Any] = ws.receive_json()
                assert invocation[0] == WampMessageType.INVOCATION
                inv_req_id = invocation[1]
                assert invocation[2] == reg_id
                # Check receive_progress in details
                assert invocation[3].get("receive_progress") is True

                # Client sends progressive YIELDs
                for i in range(2):
                    yield_msg: list[Any] = [
                        WampMessageType.YIELD,
                        inv_req_id,
                        {"progress": True},
                        [f"partial-{i}"],
                    ]
                    ws.send_json(yield_msg)
                    time.sleep(0.05)  # Let server process

                # Final YIELD
                final_yield: list[Any] = [
                    WampMessageType.YIELD,
                    inv_req_id,
                    {},
                    ["final"],
                ]
                ws.send_json(final_yield)

                time.sleep(0.1)
                do_goodbye(ws)

        assert progress_values == ["partial-0", "partial-1"]
        assert final_result == ["final"]


class TestCallCancellationE2E:
    """Call cancellation flow end-to-end via FastAPI test client."""

    def test_client_cancels_server_call(self) -> None:
        """Client sends CANCEL for a long-running server RPC; server
        replies with ERROR wamp.error.canceled."""
        hub = WampHub(realm="realm1")
        handler_started = False

        @hub.register("com.example.slow")
        async def slow_handler() -> str:
            nonlocal handler_started
            handler_started = True
            await asyncio.sleep(10)  # long operation
            return "should not reach"

        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                # Call the slow RPC
                call_msg: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {},
                    "com.example.slow",
                ]
                ws.send_json(call_msg)

                # Give the handler time to start
                time.sleep(0.2)

                # Cancel the call (kill mode)
                cancel_msg: list[Any] = [
                    WampMessageType.CANCEL,
                    1,
                    {"mode": "kill"},
                ]
                ws.send_json(cancel_msg)

                # Should receive ERROR with wamp.error.canceled
                error: list[Any] = ws.receive_json()
                assert error[0] == WampMessageType.ERROR
                assert error[1] == WampMessageType.CALL
                assert error[2] == 1  # request_id
                assert error[4] == WAMP_ERROR_CANCELED

                do_goodbye(ws)

        assert handler_started is True
        assert hub.session_count == 0

    def test_cancel_after_completion_is_ignored(self) -> None:
        """CANCEL sent after handler completes is silently ignored."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.fast")
        async def fast_handler() -> str:
            return "quick result"

        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                # Call the fast RPC
                call_msg: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {},
                    "com.example.fast",
                ]
                ws.send_json(call_msg)

                # Get the RESULT first
                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[3] == ["quick result"]

                # Now send CANCEL after completion — should be ignored
                cancel_msg: list[Any] = [
                    WampMessageType.CANCEL,
                    1,
                    {"mode": "kill"},
                ]
                ws.send_json(cancel_msg)

                # Session should still be working normally
                call2: list[Any] = [
                    WampMessageType.CALL,
                    2,
                    {},
                    "com.example.fast",
                ]
                ws.send_json(call2)
                result2: list[Any] = ws.receive_json()
                assert result2[0] == WampMessageType.RESULT
                assert result2[1] == 2
                assert result2[3] == ["quick result"]

                do_goodbye(ws)


class TestMultipleConcurrentConnections:
    """Multiple concurrent WebSocket connections with isolated sessions."""

    def test_two_clients_isolated_sessions(self) -> None:
        """Two concurrent clients each get their own session with
        independent state."""
        hub = WampHub(realm="realm1")
        session_ids: list[int] = []

        @hub.register("com.example.whoami")
        async def whoami(_caller_session_id: int | None = None) -> int:
            return _caller_session_id or 0

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            session_ids.append(session.session_id)

        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws1:
                sid1 = do_handshake(ws1)

                with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws2:
                    sid2 = do_handshake(ws2)

                    # Both should have unique session IDs
                    assert sid1 != sid2
                    assert hub.session_count == 2

                    # Client 1 calls whoami
                    call1: list[Any] = [
                        WampMessageType.CALL,
                        1,
                        {"disclose_me": True},
                        "com.example.whoami",
                    ]
                    ws1.send_json(call1)
                    r1: list[Any] = ws1.receive_json()
                    assert r1[0] == WampMessageType.RESULT
                    assert r1[3] == [sid1]

                    # Client 2 calls whoami
                    call2: list[Any] = [
                        WampMessageType.CALL,
                        1,
                        {"disclose_me": True},
                        "com.example.whoami",
                    ]
                    ws2.send_json(call2)
                    r2: list[Any] = ws2.receive_json()
                    assert r2[0] == WampMessageType.RESULT
                    assert r2[3] == [sid2]

                    do_goodbye(ws2)

                assert hub.session_count == 1
                do_goodbye(ws1)

        assert hub.session_count == 0
        assert len(session_ids) == 2
        assert session_ids[0] != session_ids[1]

    def test_isolated_subscriptions_across_clients(self) -> None:
        """Subscriptions are per-session — one client's subscription
        does not affect another."""
        hub = WampHub(realm="realm1")
        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws1:
                do_handshake(ws1)

                # Client 1 subscribes
                sub1: list[Any] = [
                    WampMessageType.SUBSCRIBE,
                    1,
                    {},
                    "com.example.topic",
                ]
                ws1.send_json(sub1)
                subscribed1: list[Any] = ws1.receive_json()
                assert subscribed1[0] == WampMessageType.SUBSCRIBED
                sub_id_1 = subscribed1[2]

                with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws2:
                    do_handshake(ws2)

                    # Client 2 subscribes to same topic
                    sub2: list[Any] = [
                        WampMessageType.SUBSCRIBE,
                        1,
                        {},
                        "com.example.topic",
                    ]
                    ws2.send_json(sub2)
                    subscribed2: list[Any] = ws2.receive_json()
                    assert subscribed2[0] == WampMessageType.SUBSCRIBED
                    sub_id_2 = subscribed2[2]

                    # Subscription IDs should be different (hub-global unique)
                    assert sub_id_1 != sub_id_2

                    # Client 2 unsubscribes — should not affect client 1
                    unsub2: list[Any] = [
                        WampMessageType.UNSUBSCRIBE,
                        2,
                        sub_id_2,
                    ]
                    ws2.send_json(unsub2)
                    unsubscribed2: list[Any] = ws2.receive_json()
                    assert unsubscribed2[0] == WampMessageType.UNSUBSCRIBED

                    do_goodbye(ws2)

                # Client 1's subscription should still work
                # (unsubscribe from client 1 side)
                unsub1: list[Any] = [
                    WampMessageType.UNSUBSCRIBE,
                    3,
                    sub_id_1,
                ]
                ws1.send_json(unsub1)
                unsubscribed1: list[Any] = ws1.receive_json()
                assert unsubscribed1[0] == WampMessageType.UNSUBSCRIBED

                do_goodbye(ws1)

    def test_isolated_client_rpc_registrations(self) -> None:
        """Client RPC registrations are per-session — both clients can
        register the same URI independently."""
        hub = WampHub(realm="realm1")
        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws1:
                do_handshake(ws1)

                # Client 1 registers a procedure
                reg1: list[Any] = [
                    WampMessageType.REGISTER,
                    1,
                    {},
                    "com.shared.proc",
                ]
                ws1.send_json(reg1)
                registered1: list[Any] = ws1.receive_json()
                assert registered1[0] == WampMessageType.REGISTERED
                reg_id_1 = registered1[2]

                with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws2:
                    do_handshake(ws2)

                    # Client 2 registers the same procedure name — no conflict
                    reg2: list[Any] = [
                        WampMessageType.REGISTER,
                        1,
                        {},
                        "com.shared.proc",
                    ]
                    ws2.send_json(reg2)
                    registered2: list[Any] = ws2.receive_json()
                    assert registered2[0] == WampMessageType.REGISTERED
                    reg_id_2 = registered2[2]

                    # Registration IDs should be unique
                    assert reg_id_1 != reg_id_2

                    do_goodbye(ws2)

                # Client 1's registration still valid after client 2 disconnects
                unreg1: list[Any] = [
                    WampMessageType.UNREGISTER,
                    2,
                    reg_id_1,
                ]
                ws1.send_json(unreg1)
                unregistered1: list[Any] = ws1.receive_json()
                assert unregistered1[0] == WampMessageType.UNREGISTERED

                do_goodbye(ws1)


class TestErrorPaths:
    """Error paths: invalid messages, protocol violations, disconnects
    during pending calls."""

    def test_abort_on_non_hello_first_message(self) -> None:
        """If first message is not HELLO, server sends ABORT."""
        hub = WampHub(realm="realm1")
        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                # Send GOODBYE as the first message instead of HELLO
                bad: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
                ws.send_json(bad)
                abort: list[Any] = ws.receive_json()
                assert abort[0] == WampMessageType.ABORT

        assert hub.session_count == 0

    def test_abort_on_wrong_realm(self) -> None:
        """ABORT sent when client specifies wrong realm."""
        hub = WampHub(realm="realm1")
        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                hello: list[Any] = [
                    WampMessageType.HELLO,
                    "wrong_realm",
                    {"roles": {"caller": {}}},
                ]
                ws.send_json(hello)
                abort: list[Any] = ws.receive_json()
                assert abort[0] == WampMessageType.ABORT

        assert hub.session_count == 0

    def test_call_nonexistent_procedure_returns_error(self) -> None:
        """CALL to unregistered procedure returns ERROR with no_such_procedure."""
        hub = WampHub(realm="realm1")
        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                call_msg: list[Any] = [
                    WampMessageType.CALL,
                    42,
                    {},
                    "com.nonexistent.procedure",
                ]
                ws.send_json(call_msg)

                error: list[Any] = ws.receive_json()
                assert error[0] == WampMessageType.ERROR
                assert error[1] == WampMessageType.CALL
                assert error[2] == 42  # request_id preserved
                assert error[4] == WAMP_ERROR_NO_SUCH_PROCEDURE

                do_goodbye(ws)

    def test_unsubscribe_nonexistent_returns_error(self) -> None:
        """UNSUBSCRIBE for a non-existent subscription returns ERROR."""
        hub = WampHub(realm="realm1")
        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                unsub_msg: list[Any] = [
                    WampMessageType.UNSUBSCRIBE,
                    1,
                    999999,  # non-existent subscription_id
                ]
                ws.send_json(unsub_msg)

                error: list[Any] = ws.receive_json()
                assert error[0] == WampMessageType.ERROR
                assert error[1] == WampMessageType.UNSUBSCRIBE
                assert error[4] == WAMP_ERROR_NO_SUCH_SUBSCRIPTION

                do_goodbye(ws)

    def test_unregister_nonexistent_returns_error(self) -> None:
        """UNREGISTER for a non-existent registration returns ERROR."""
        hub = WampHub(realm="realm1")
        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                unreg_msg: list[Any] = [
                    WampMessageType.UNREGISTER,
                    1,
                    999999,  # non-existent registration_id
                ]
                ws.send_json(unreg_msg)

                error: list[Any] = ws.receive_json()
                assert error[0] == WampMessageType.ERROR
                assert error[1] == WampMessageType.UNREGISTER
                assert error[4] == WAMP_ERROR_NO_SUCH_PROCEDURE

                do_goodbye(ws)

    def test_handler_exception_returns_runtime_error(self) -> None:
        """RPC handler that raises returns ERROR with runtime_error."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.fail")
        async def fail() -> None:
            raise ValueError("Something went wrong")

        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                call_msg: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {},
                    "com.example.fail",
                ]
                ws.send_json(call_msg)

                error: list[Any] = ws.receive_json()
                assert error[0] == WampMessageType.ERROR
                assert error[1] == WampMessageType.CALL
                assert error[2] == 1
                assert error[4] == WAMP_ERROR_RUNTIME_ERROR
                assert "Something went wrong" in error[5][0]

                do_goodbye(ws)

    def test_multiple_sequential_calls_different_request_ids(self) -> None:
        """Multiple sequential CALL messages with different request_ids
        all get correctly matched responses."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.double")
        async def double(n: int) -> int:
            return n * 2

        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                for i in range(1, 6):
                    call_msg: list[Any] = [
                        WampMessageType.CALL,
                        i,
                        {},
                        "com.example.double",
                        [i],
                    ]
                    ws.send_json(call_msg)
                    result: list[Any] = ws.receive_json()
                    assert result[0] == WampMessageType.RESULT
                    assert result[1] == i
                    assert result[3] == [i * 2]

                do_goodbye(ws)

    def test_disconnect_cleans_up_session(self) -> None:
        """Session is properly removed from hub when client disconnects
        without GOODBYE."""
        hub = WampHub(realm="realm1")
        close_called: list[bool] = []

        @hub.on_session_close
        async def on_close(session: WampSession) -> None:
            close_called.append(True)

        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)
                # Just exit the context — no GOODBYE

        # Hub should have cleaned up
        assert hub.session_count == 0
        assert len(close_called) == 1


class TestClassBasedServiceIntegration:
    """Integration tests for class-based WampService with @rpc and
    @subscribe decorators."""

    def test_service_rpc_invocation(self) -> None:
        """Class-based service RPCs are callable through the WebSocket."""
        hub = WampHub(realm="realm1")

        class MathService(WampService):
            prefix = "com.math"

            @rpc("add")
            async def add_numbers(self, a: int, b: int) -> int:
                return a + b

            @rpc()
            async def multiply(self, x: int, y: int) -> int:
                return x * y

        hub.register_service(MathService())
        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                # Call com.math.add
                call1: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {},
                    "com.math.add",
                    [5, 3],
                ]
                ws.send_json(call1)
                r1: list[Any] = ws.receive_json()
                assert r1[0] == WampMessageType.RESULT
                assert r1[3] == [8]

                # Call com.math.multiply (inferred URI from method name)
                call2: list[Any] = [
                    WampMessageType.CALL,
                    2,
                    {},
                    "com.math.multiply",
                    [6, 7],
                ]
                ws.send_json(call2)
                r2: list[Any] = ws.receive_json()
                assert r2[0] == WampMessageType.RESULT
                assert r2[3] == [42]

                do_goodbye(ws)

    def test_service_subscribe_handler(self) -> None:
        """Class-based service @subscribe handlers are invoked on client PUBLISH."""
        hub = WampHub(realm="realm1")
        received_events: list[dict[str, Any]] = []

        class EventService(WampService):
            prefix = "com.events"

            @subscribe("user.login")
            async def on_login(self, username: str) -> None:
                received_events.append({"username": username})

        hub.register_service(EventService())
        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                pub_msg: list[Any] = [
                    WampMessageType.PUBLISH,
                    1,
                    {"acknowledge": True},
                    "com.events.user.login",
                    ["alice"],
                ]
                ws.send_json(pub_msg)

                published: list[Any] = ws.receive_json()
                assert published[0] == WampMessageType.PUBLISHED

                do_goodbye(ws)

        assert len(received_events) == 1
        assert received_events[0]["username"] == "alice"

    def test_service_and_standalone_coexist(self) -> None:
        """Service-registered and standalone-registered RPCs coexist."""
        hub = WampHub(realm="realm1")

        class GreetService(WampService):
            prefix = "com.greet"

            @rpc("hello")
            async def hello(self, name: str) -> str:
                return f"Hello, {name}"

        hub.register_service(GreetService())

        @hub.register("com.standalone.echo")
        async def echo(msg: str) -> str:
            return msg

        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                # Call service RPC
                call1: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {},
                    "com.greet.hello",
                    ["World"],
                ]
                ws.send_json(call1)
                r1: list[Any] = ws.receive_json()
                assert r1[0] == WampMessageType.RESULT
                assert r1[3] == ["Hello, World"]

                # Call standalone RPC
                call2: list[Any] = [
                    WampMessageType.CALL,
                    2,
                    {},
                    "com.standalone.echo",
                    ["test"],
                ]
                ws.send_json(call2)
                r2: list[Any] = ws.receive_json()
                assert r2[0] == WampMessageType.RESULT
                assert r2[3] == ["test"]

                do_goodbye(ws)


class TestSyncHandlerIntegration:
    """Sync handlers should work via asyncio.to_thread."""

    def test_sync_rpc_handler(self) -> None:
        """Sync (non-async) RPC handler works through full stack."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.sync_add")
        def sync_add(a: int, b: int) -> int:
            return a + b

        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                call_msg: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {},
                    "com.example.sync_add",
                    [100, 200],
                ]
                ws.send_json(call_msg)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[3] == [300]

                do_goodbye(ws)


class TestCallerIdentificationE2E:
    """caller_identification: disclose_me option passes session ID to handler."""

    def test_disclose_me_passes_session_id(self) -> None:
        """CALL with disclose_me: true makes _caller_session_id available."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.identify")
        async def identify(_caller_session_id: int | None = None) -> int:
            return _caller_session_id or 0

        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                session_id = do_handshake(ws)

                call_msg: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {"disclose_me": True},
                    "com.example.identify",
                ]
                ws.send_json(call_msg)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[3] == [session_id]

                do_goodbye(ws)

    def test_without_disclose_me_no_session_id(self) -> None:
        """CALL without disclose_me does not pass _caller_session_id."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.identify2")
        async def identify(_caller_session_id: int | None = None) -> int:
            return _caller_session_id or -1

        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                call_msg: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {},
                    "com.example.identify2",
                ]
                ws.send_json(call_msg)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[3] == [-1]

                do_goodbye(ws)


class TestCallTimeoutE2E:
    """call_timeout: client sends timeout in CALL options."""

    def test_call_timeout_sends_canceled_error(self) -> None:
        """CALL with timeout option causes server to cancel handler
        and return ERROR wamp.error.canceled after the timeout."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.sleepy")
        async def sleepy() -> str:
            await asyncio.sleep(10)
            return "awake"

        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                do_handshake(ws)

                # CALL with 100ms timeout
                call_msg: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {"timeout": 100},
                    "com.example.sleepy",
                ]
                ws.send_json(call_msg)

                error: list[Any] = ws.receive_json()
                assert error[0] == WampMessageType.ERROR
                assert error[1] == WampMessageType.CALL
                assert error[2] == 1
                assert error[4] == WAMP_ERROR_CANCELED

                do_goodbye(ws)
