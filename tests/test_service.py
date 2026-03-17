"""Tests for class-based WampService with @rpc decorator (US-009).

Covers:
- WampService with prefix class attribute
- @rpc(uri) marks methods as RPC handlers; @rpc() infers URI from method name
- Full URI = prefix + '.' + uri (or prefix + '.' + method_name)
- Both async and sync methods supported (sync via asyncio.to_thread())
- WampHub.register_service(instance) registers all @rpc-marked methods
- Service instance gets hub attribute set during registration
- End-to-end invocation via FastAPI TestClient
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi import WebSocket as FastAPIWebSocket
from starlette.testclient import TestClient

from fastapi_headless_wamp.hub import WampHub
from fastapi_headless_wamp.protocol import (
    WAMP_ERROR_NO_SUCH_PROCEDURE,
    WAMP_ERROR_RUNTIME_ERROR,
    WampMessageType,
)
from fastapi_headless_wamp.service import WampService, rpc

# ---------------------------------------------------------------------------
# Helper: FastAPI app + handshake
# ---------------------------------------------------------------------------


def _make_app(hub: WampHub) -> FastAPI:
    app = FastAPI()

    @app.websocket("/ws")
    async def wamp_endpoint(websocket: FastAPIWebSocket) -> None:
        await hub.handle_websocket(websocket)

    return app


def _do_handshake(ws: Any) -> int:
    """Send HELLO, receive WELCOME, return session_id."""
    hello: list[Any] = [
        WampMessageType.HELLO,
        "realm1",
        {"roles": {"caller": {}, "callee": {}}},
    ]
    ws.send_json(hello)
    welcome: list[Any] = ws.receive_json()
    assert welcome[0] == WampMessageType.WELCOME
    session_id: int = welcome[1]
    return session_id


def _send_goodbye(ws: Any) -> list[Any]:
    goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
    ws.send_json(goodbye)
    reply: list[Any] = ws.receive_json()
    return reply


# ---------------------------------------------------------------------------
# Sample services for testing
# ---------------------------------------------------------------------------


class MathService(WampService):
    """Math service with explicit prefix."""

    prefix = "com.example.math"

    @rpc()
    async def add(self, a: int, b: int) -> int:
        return a + b

    @rpc("multiply")
    async def mul(self, a: int, b: int) -> int:
        return a * b

    @rpc()
    def subtract(self, a: int, b: int) -> int:
        """Sync method — should run via asyncio.to_thread."""
        return a - b


class NoPrefixService(WampService):
    """Service with no prefix; URI is just the method name or explicit URI."""

    @rpc("greet")
    async def greet(self, name: str) -> str:
        return f"hello {name}"

    @rpc()
    async def echo(self, value: Any) -> Any:
        return value


class HubAccessService(WampService):
    """Service that accesses self.hub."""

    prefix = "com.example.hub"

    @rpc()
    async def session_count(self) -> int:
        if self.hub is None:
            return -1
        return self.hub.session_count


class FailingService(WampService):
    """Service with a handler that raises."""

    prefix = "com.example.fail"

    @rpc()
    async def boom(self) -> None:
        raise RuntimeError("kaboom")


# ===========================================================================
# Test: @rpc decorator marks methods
# ===========================================================================


class TestRpcDecorator:
    """@rpc() decorator sets _rpc_uri attribute on methods."""

    def test_rpc_with_explicit_uri(self) -> None:
        svc = MathService()
        assert svc.mul._rpc_uri == "multiply"

    def test_rpc_with_inferred_uri(self) -> None:
        svc = MathService()
        # @rpc() with no args sets _rpc_uri = None (infer from name)
        assert svc.add._rpc_uri is None

    def test_non_rpc_method_has_no_marker(self) -> None:
        svc = MathService()
        assert not hasattr(svc.__init__, "_rpc_uri")


# ===========================================================================
# Test: register_service introspection and URI resolution
# ===========================================================================


class TestRegisterService:
    """WampHub.register_service registers all @rpc-marked methods."""

    def test_registers_all_rpcs(self) -> None:
        hub = WampHub(realm="realm1")
        svc = MathService()
        hub.register_service(svc)

        rpcs: dict[str, Any] = hub._server_rpcs
        assert "com.example.math.add" in rpcs
        assert "com.example.math.multiply" in rpcs
        assert "com.example.math.subtract" in rpcs

    def test_prefix_resolution_explicit_uri(self) -> None:
        hub = WampHub(realm="realm1")
        svc = MathService()
        hub.register_service(svc)

        rpcs: dict[str, Any] = hub._server_rpcs
        # "multiply" is the explicit URI from @rpc("multiply")
        assert "com.example.math.multiply" in rpcs

    def test_prefix_resolution_inferred_uri(self) -> None:
        hub = WampHub(realm="realm1")
        svc = MathService()
        hub.register_service(svc)

        rpcs: dict[str, Any] = hub._server_rpcs
        # "add" is inferred from method name
        assert "com.example.math.add" in rpcs

    def test_no_prefix_service(self) -> None:
        hub = WampHub(realm="realm1")
        svc = NoPrefixService()
        hub.register_service(svc)

        rpcs: dict[str, Any] = hub._server_rpcs
        assert "greet" in rpcs
        assert "echo" in rpcs

    def test_hub_attribute_set(self) -> None:
        hub = WampHub(realm="realm1")
        svc = MathService()
        assert svc.hub is None
        hub.register_service(svc)
        assert svc.hub is hub

    def test_multiple_services(self) -> None:
        hub = WampHub(realm="realm1")
        svc1 = MathService()
        svc2 = NoPrefixService()
        hub.register_service(svc1)
        hub.register_service(svc2)

        rpcs: dict[str, Any] = hub._server_rpcs
        # Math service RPCs
        assert "com.example.math.add" in rpcs
        assert "com.example.math.multiply" in rpcs
        # No-prefix service RPCs
        assert "greet" in rpcs
        assert "echo" in rpcs

    def test_service_and_standalone_rpc_coexist(self) -> None:
        hub = WampHub(realm="realm1")

        @hub.register("com.example.standalone")
        async def standalone() -> str:
            return "standalone"

        svc = MathService()
        hub.register_service(svc)

        rpcs: dict[str, Any] = hub._server_rpcs
        assert "com.example.standalone" in rpcs
        assert "com.example.math.add" in rpcs


# ===========================================================================
# Test: End-to-end invocation via FastAPI TestClient
# ===========================================================================


class TestServiceInvocation:
    """Service RPC handlers are invoked correctly via CALL messages."""

    def test_async_method_invocation(self) -> None:
        hub = WampHub(realm="realm1")
        svc = MathService()
        hub.register_service(svc)
        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {},
                    "com.example.math.add",
                    [10, 20],
                ]
                ws.send_json(call)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 1
                assert result[3] == [30]

                _send_goodbye(ws)

    def test_explicit_uri_invocation(self) -> None:
        hub = WampHub(realm="realm1")
        svc = MathService()
        hub.register_service(svc)
        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    2,
                    {},
                    "com.example.math.multiply",
                    [5, 6],
                ]
                ws.send_json(call)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 2
                assert result[3] == [30]

                _send_goodbye(ws)

    def test_sync_method_invocation(self) -> None:
        """Sync methods run via asyncio.to_thread."""
        hub = WampHub(realm="realm1")
        svc = MathService()
        hub.register_service(svc)
        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    3,
                    {},
                    "com.example.math.subtract",
                    [50, 30],
                ]
                ws.send_json(call)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 3
                assert result[3] == [20]

                _send_goodbye(ws)

    def test_no_prefix_invocation(self) -> None:
        hub = WampHub(realm="realm1")
        svc = NoPrefixService()
        hub.register_service(svc)
        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    4,
                    {},
                    "greet",
                    ["world"],
                ]
                ws.send_json(call)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 4
                assert result[3] == ["hello world"]

                _send_goodbye(ws)

    def test_hub_access_from_service(self) -> None:
        """Service methods can access self.hub."""
        hub = WampHub(realm="realm1")
        svc = HubAccessService()
        hub.register_service(svc)
        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    5,
                    {},
                    "com.example.hub.session_count",
                ]
                ws.send_json(call)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 5
                # There's 1 active session (the test client)
                assert result[3] == [1]

                _send_goodbye(ws)

    def test_service_handler_exception(self) -> None:
        """Exception in service handler sends ERROR."""
        hub = WampHub(realm="realm1")
        svc = FailingService()
        hub.register_service(svc)
        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    6,
                    {},
                    "com.example.fail.boom",
                ]
                ws.send_json(call)

                error: list[Any] = ws.receive_json()
                assert error[0] == WampMessageType.ERROR
                assert error[1] == WampMessageType.CALL
                assert error[2] == 6
                assert error[4] == WAMP_ERROR_RUNTIME_ERROR
                assert "kaboom" in error[5][0]

                _send_goodbye(ws)

    def test_nonexistent_service_rpc(self) -> None:
        """CALL for non-existent service URI sends no_such_procedure."""
        hub = WampHub(realm="realm1")
        svc = MathService()
        hub.register_service(svc)
        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    7,
                    {},
                    "com.example.math.nonexistent",
                ]
                ws.send_json(call)

                error: list[Any] = ws.receive_json()
                assert error[0] == WampMessageType.ERROR
                assert error[4] == WAMP_ERROR_NO_SUCH_PROCEDURE

                _send_goodbye(ws)

    def test_multiple_sequential_service_calls(self) -> None:
        """Multiple calls to service RPCs in sequence."""
        hub = WampHub(realm="realm1")
        svc = MathService()
        hub.register_service(svc)
        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                # add
                ws.send_json(
                    [WampMessageType.CALL, 1, {}, "com.example.math.add", [1, 2]]
                )
                r1: list[Any] = ws.receive_json()
                assert r1[0] == WampMessageType.RESULT
                assert r1[3] == [3]

                # multiply
                ws.send_json(
                    [
                        WampMessageType.CALL,
                        2,
                        {},
                        "com.example.math.multiply",
                        [3, 4],
                    ]
                )
                r2: list[Any] = ws.receive_json()
                assert r2[0] == WampMessageType.RESULT
                assert r2[3] == [12]

                # subtract (sync)
                ws.send_json(
                    [
                        WampMessageType.CALL,
                        3,
                        {},
                        "com.example.math.subtract",
                        [10, 3],
                    ]
                )
                r3: list[Any] = ws.receive_json()
                assert r3[0] == WampMessageType.RESULT
                assert r3[3] == [7]

                _send_goodbye(ws)
