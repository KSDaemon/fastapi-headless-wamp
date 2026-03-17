"""Tests for server-side RPC registration and dispatch (US-008).

Covers:
- @wamp.register(uri) decorator stores handler on hub
- Client CALL for registered URI invokes handler and returns RESULT
- Both async and sync handlers supported (sync via asyncio.to_thread)
- Exception in handler sends ERROR with wamp.error.runtime_error
- CALL for non-existent URI sends ERROR with wamp.error.no_such_procedure
- Request IDs correctly matched in RESULT/ERROR
- call_timeout: server cancels handler after timeout and sends ERROR wamp.error.canceled
- caller_identification: disclose_me in CALL options passes caller session ID
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI
from fastapi import WebSocket as FastAPIWebSocket
from starlette.testclient import TestClient

from fastapi_headless_wamp.errors import WampNoSuchProcedure
from fastapi_headless_wamp.hub import WampHub
from fastapi_headless_wamp.protocol import (
    WAMP_ERROR_CANCELED,
    WAMP_ERROR_NO_SUCH_PROCEDURE,
    WAMP_ERROR_RUNTIME_ERROR,
    WampMessageType,
)

# ---------------------------------------------------------------------------
# Mock WebSocket (consistent with test_hub.py)
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
    """Parse JSON message at *index* from sent_texts."""
    result: list[Any] = json.loads(ws.sent_texts[index])
    return result


def _make_app(hub: WampHub) -> FastAPI:
    """Create a FastAPI app with a /ws WAMP endpoint."""
    app = FastAPI()

    @app.websocket("/ws")
    async def wamp_endpoint(websocket: FastAPIWebSocket) -> None:
        await hub.handle_websocket(websocket)

    return app


# ---------------------------------------------------------------------------
# Helper: do handshake + send CALL via FastAPI TestClient
# ---------------------------------------------------------------------------


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
    """Send GOODBYE, receive reply."""
    goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
    ws.send_json(goodbye)
    reply: list[Any] = ws.receive_json()
    return reply


# ===========================================================================
# Test: @wamp.register(uri) decorator
# ===========================================================================


class TestRegisterDecorator:
    """@wamp.register(uri) stores handler on hub."""

    def test_register_stores_handler(self) -> None:
        hub = WampHub(realm="realm1")

        @hub.register("com.example.add")
        async def add(a: int, b: int) -> int:
            return a + b

        # Access via internal dict (cast to Any for test introspection)
        rpcs: dict[str, Any] = hub._server_rpcs
        assert "com.example.add" in rpcs
        assert rpcs["com.example.add"] is add

    def test_register_multiple_handlers(self) -> None:
        hub = WampHub(realm="realm1")

        @hub.register("com.example.add")
        async def add(a: int, b: int) -> int:
            return a + b

        @hub.register("com.example.mul")
        async def mul(a: int, b: int) -> int:
            return a * b

        rpcs: dict[str, Any] = hub._server_rpcs
        assert len(rpcs) == 2
        assert "com.example.add" in rpcs
        assert "com.example.mul" in rpcs

    def test_register_returns_original_function(self) -> None:
        hub = WampHub(realm="realm1")

        @hub.register("com.example.greet")
        async def greet(name: str) -> str:
            return f"hello {name}"

        # The decorator should return the function unchanged
        assert greet.__name__ == "greet"

    def test_register_sync_handler(self) -> None:
        hub = WampHub(realm="realm1")

        @hub.register("com.example.sync_add")
        def sync_add(a: int, b: int) -> int:
            return a + b

        rpcs: dict[str, Any] = hub._server_rpcs
        assert "com.example.sync_add" in rpcs


# ===========================================================================
# Test: Successful CALL -> RESULT (async handler)
# ===========================================================================


class TestCallSuccess:
    """Client CALL for registered URI returns RESULT."""

    def test_async_handler_success(self) -> None:
        hub = WampHub(realm="realm1")

        @hub.register("com.example.add")
        async def add(a: int, b: int) -> int:
            return a + b

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                # Send CALL
                call: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {},
                    "com.example.add",
                    [3, 4],
                ]
                ws.send_json(call)

                # Receive RESULT
                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 1  # request_id matches
                assert result[2] == {}  # details
                assert result[3] == [7]  # return value wrapped in list

                _send_goodbye(ws)

    def test_handler_with_kwargs(self) -> None:
        hub = WampHub(realm="realm1")

        @hub.register("com.example.greet")
        async def greet(name: str, greeting: str = "hello") -> str:
            return f"{greeting} {name}"

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    42,
                    {},
                    "com.example.greet",
                    [],
                    {"name": "world", "greeting": "hi"},
                ]
                ws.send_json(call)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 42
                assert result[3] == ["hi world"]

                _send_goodbye(ws)

    def test_handler_returns_none(self) -> None:
        hub = WampHub(realm="realm1")

        @hub.register("com.example.noop")
        async def noop() -> None:
            pass

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [WampMessageType.CALL, 10, {}, "com.example.noop"]
                ws.send_json(call)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 10
                # No args list when returning None
                assert len(result) == 3

                _send_goodbye(ws)

    def test_request_id_correctly_matched(self) -> None:
        """Multiple CALLs have their request IDs correctly matched in RESULTs."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.echo")
        async def echo(val: Any) -> Any:
            return val

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                for req_id in [100, 200, 300]:
                    call: list[Any] = [
                        WampMessageType.CALL,
                        req_id,
                        {},
                        "com.example.echo",
                        [req_id],
                    ]
                    ws.send_json(call)
                    result: list[Any] = ws.receive_json()
                    assert result[0] == WampMessageType.RESULT
                    assert result[1] == req_id
                    assert result[3] == [req_id]

                _send_goodbye(ws)


# ===========================================================================
# Test: Sync handler support
# ===========================================================================


class TestSyncHandler:
    """Sync functions run via asyncio.to_thread."""

    def test_sync_handler_success(self) -> None:
        hub = WampHub(realm="realm1")

        @hub.register("com.example.sync_add")
        def sync_add(a: int, b: int) -> int:
            return a + b

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {},
                    "com.example.sync_add",
                    [10, 20],
                ]
                ws.send_json(call)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 1
                assert result[3] == [30]

                _send_goodbye(ws)

    def test_sync_handler_exception(self) -> None:
        hub = WampHub(realm="realm1")

        @hub.register("com.example.sync_fail")
        def sync_fail() -> None:
            raise ValueError("sync boom")

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    5,
                    {},
                    "com.example.sync_fail",
                ]
                ws.send_json(call)

                error: list[Any] = ws.receive_json()
                assert error[0] == WampMessageType.ERROR
                assert error[2] == 5
                assert error[4] == WAMP_ERROR_RUNTIME_ERROR

                _send_goodbye(ws)


# ===========================================================================
# Test: Exception handling
# ===========================================================================


class TestExceptionHandling:
    """Exceptions in handler produce ERROR messages."""

    def test_runtime_error(self) -> None:
        hub = WampHub(realm="realm1")

        @hub.register("com.example.fail")
        async def fail() -> None:
            raise RuntimeError("something went wrong")

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [WampMessageType.CALL, 7, {}, "com.example.fail"]
                ws.send_json(call)

                error: list[Any] = ws.receive_json()
                assert error[0] == WampMessageType.ERROR
                assert error[1] == WampMessageType.CALL
                assert error[2] == 7
                assert error[4] == WAMP_ERROR_RUNTIME_ERROR
                # Error message in args
                assert len(error) >= 6
                assert "something went wrong" in error[5][0]

                _send_goodbye(ws)

    def test_wamp_error_with_uri(self) -> None:
        """WampError subclasses send their own URI."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.wamp_err")
        async def wamp_err() -> None:
            raise WampNoSuchProcedure("custom message")

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    8,
                    {},
                    "com.example.wamp_err",
                ]
                ws.send_json(call)

                error: list[Any] = ws.receive_json()
                assert error[0] == WampMessageType.ERROR
                assert error[2] == 8
                assert error[4] == "wamp.error.no_such_procedure"

                _send_goodbye(ws)

    def test_wamp_error_with_payload(self) -> None:
        """WampError carries args and kwargs in the ERROR message."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.wamp_err_payload")
        async def wamp_err_payload() -> None:
            raise WampNoSuchProcedure(
                "not found",
                args=["extra_arg"],
                kwargs={"detail": "more info"},
            )

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    9,
                    {},
                    "com.example.wamp_err_payload",
                ]
                ws.send_json(call)

                error: list[Any] = ws.receive_json()
                assert error[0] == WampMessageType.ERROR
                assert error[2] == 9
                assert error[4] == "wamp.error.no_such_procedure"
                assert error[5] == ["extra_arg"]
                assert error[6] == {"detail": "more info"}

                _send_goodbye(ws)


# ===========================================================================
# Test: No such procedure
# ===========================================================================


class TestNoSuchProcedure:
    """CALL for non-existent URI sends ERROR wamp.error.no_such_procedure."""

    def test_no_such_procedure(self) -> None:
        hub = WampHub(realm="realm1")
        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    99,
                    {},
                    "com.nonexistent.procedure",
                ]
                ws.send_json(call)

                error: list[Any] = ws.receive_json()
                assert error[0] == WampMessageType.ERROR
                assert error[1] == WampMessageType.CALL
                assert error[2] == 99
                assert error[4] == WAMP_ERROR_NO_SUCH_PROCEDURE

                _send_goodbye(ws)


# ===========================================================================
# Test: call_timeout
# ===========================================================================


class TestCallTimeout:
    """call_timeout: server cancels handler after timeout."""

    def test_timeout_sends_canceled_error(self) -> None:
        hub = WampHub(realm="realm1")

        @hub.register("com.example.slow")
        async def slow() -> str:
            await asyncio.sleep(10)
            return "done"

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                # Send CALL with 100ms timeout
                call: list[Any] = [
                    WampMessageType.CALL,
                    20,
                    {"timeout": 100},
                    "com.example.slow",
                ]
                ws.send_json(call)

                error: list[Any] = ws.receive_json()
                assert error[0] == WampMessageType.ERROR
                assert error[1] == WampMessageType.CALL
                assert error[2] == 20
                assert error[4] == WAMP_ERROR_CANCELED

                _send_goodbye(ws)

    def test_no_timeout_when_not_set(self) -> None:
        """Handler completes normally when no timeout specified."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.fast")
        async def fast() -> str:
            return "quick"

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    21,
                    {},
                    "com.example.fast",
                ]
                ws.send_json(call)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 21
                assert result[3] == ["quick"]

                _send_goodbye(ws)

    def test_timeout_zero_ignored(self) -> None:
        """timeout=0 is treated as no timeout."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.ok")
        async def ok() -> str:
            return "ok"

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    22,
                    {"timeout": 0},
                    "com.example.ok",
                ]
                ws.send_json(call)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 22

                _send_goodbye(ws)


# ===========================================================================
# Test: caller_identification
# ===========================================================================


class TestCallerIdentification:
    """disclose_me in CALL options passes caller session ID."""

    def test_disclose_me_passes_session_id(self) -> None:
        hub = WampHub(realm="realm1")
        received_caller_id: list[int] = []

        @hub.register("com.example.who_am_i")
        async def who_am_i(_caller_session_id: int = 0) -> int:
            received_caller_id.append(_caller_session_id)
            return _caller_session_id

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                session_id = _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    30,
                    {"disclose_me": True},
                    "com.example.who_am_i",
                ]
                ws.send_json(call)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 30
                assert result[3] == [session_id]
                assert received_caller_id[0] == session_id

                _send_goodbye(ws)

    def test_no_disclose_me_no_caller_id(self) -> None:
        """Without disclose_me, _caller_session_id is not passed."""
        hub = WampHub(realm="realm1")
        received_caller_id: list[int] = []

        @hub.register("com.example.who_am_i2")
        async def who_am_i2(_caller_session_id: int = 0) -> int:
            received_caller_id.append(_caller_session_id)
            return _caller_session_id

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    31,
                    {},  # no disclose_me
                    "com.example.who_am_i2",
                ]
                ws.send_json(call)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 31
                # Default value 0 used
                assert result[3] == [0]
                assert received_caller_id[0] == 0

                _send_goodbye(ws)


# ===========================================================================
# Test: Multiple calls in sequence
# ===========================================================================


class TestMultipleCalls:
    """Multiple sequential CALLs work correctly."""

    def test_multiple_calls_sequential(self) -> None:
        hub = WampHub(realm="realm1")

        @hub.register("com.example.double")
        async def double(x: int) -> int:
            return x * 2

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                for i in range(5):
                    call: list[Any] = [
                        WampMessageType.CALL,
                        i + 1,
                        {},
                        "com.example.double",
                        [i],
                    ]
                    ws.send_json(call)
                    result: list[Any] = ws.receive_json()
                    assert result[0] == WampMessageType.RESULT
                    assert result[1] == i + 1
                    assert result[3] == [i * 2]

                _send_goodbye(ws)

    def test_mixed_success_and_error_calls(self) -> None:
        """Mix of successful and failing calls."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.div")
        async def div(a: int, b: int) -> float:
            return a / b

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                # Successful call
                call1: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {},
                    "com.example.div",
                    [10, 2],
                ]
                ws.send_json(call1)
                result1: list[Any] = ws.receive_json()
                assert result1[0] == WampMessageType.RESULT
                assert result1[3] == [5.0]

                # Failing call (divide by zero)
                call2: list[Any] = [
                    WampMessageType.CALL,
                    2,
                    {},
                    "com.example.div",
                    [10, 0],
                ]
                ws.send_json(call2)
                error: list[Any] = ws.receive_json()
                assert error[0] == WampMessageType.ERROR
                assert error[2] == 2
                assert error[4] == WAMP_ERROR_RUNTIME_ERROR

                # Another successful call after error
                call3: list[Any] = [
                    WampMessageType.CALL,
                    3,
                    {},
                    "com.example.div",
                    [20, 4],
                ]
                ws.send_json(call3)
                result3: list[Any] = ws.receive_json()
                assert result3[0] == WampMessageType.RESULT
                assert result3[3] == [5.0]

                _send_goodbye(ws)


# ===========================================================================
# Test: Mock-based unit tests (without FastAPI)
# ===========================================================================


class TestCallHandlerUnit:
    """Unit tests for _handle_call using MockWebSocket directly."""

    async def test_handle_call_async(self) -> None:
        hub = WampHub(realm="realm1")

        @hub.register("com.example.add")
        async def add(a: int, b: int) -> int:
            return a + b

        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        # Set up handshake
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        call: list[Any] = [WampMessageType.CALL, 1, {}, "com.example.add", [5, 3]]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]

        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(call))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Messages: WELCOME, RESULT, GOODBYE reply (order may vary due to
        # task-based handler execution — RESULT and GOODBYE may be interleaved)
        assert len(ws.sent_texts) == 3

        # Find the RESULT message regardless of position
        result_msgs = [
            json.loads(t)
            for t in ws.sent_texts
            if json.loads(t)[0] == WampMessageType.RESULT
        ]
        assert len(result_msgs) == 1
        result = result_msgs[0]
        assert result[0] == WampMessageType.RESULT
        assert result[1] == 1
        assert result[3] == [8]

    async def test_handle_call_no_procedure(self) -> None:
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        call: list[Any] = [WampMessageType.CALL, 2, {}, "com.doesnt.exist"]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]

        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(call))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Messages: WELCOME, ERROR, GOODBYE reply
        assert len(ws.sent_texts) == 3

        error = json.loads(ws.sent_texts[1])
        assert error[0] == WampMessageType.ERROR
        assert error[1] == WampMessageType.CALL
        assert error[2] == 2
        assert error[4] == WAMP_ERROR_NO_SUCH_PROCEDURE
