"""Tests for server calling client-registered RPCs (US-011).

Covers:
- session.call(uri, args, kwargs, timeout) sends INVOCATION and awaits result
- Successful call: client responds with YIELD -> result returned
- Client error response: client responds with ERROR -> WampError raised
- Timeout: client doesn't respond in time -> WampCallTimeoutError raised
- Non-existent procedure: URI not in client_rpcs -> WampNoSuchProcedureError raised
- Disconnect during pending call: future rejected with WampError
- Server-initiated request IDs use a separate counter from client-initiated IDs
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI
from fastapi import WebSocket as FastAPIWebSocket
from starlette.testclient import TestClient

from fastapi_headless_wamp.errors import (
    WampCallTimeoutError,
    WampError,
    WampNoSuchProcedureError,
)
from fastapi_headless_wamp.hub import WampHub
from fastapi_headless_wamp.protocol import (
    WAMP_ERROR_RUNTIME_ERROR,
    WampMessageType,
)
from fastapi_headless_wamp.session import WampSession

# ---------------------------------------------------------------------------
# Mock WebSocket helper
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
# Helpers
# ---------------------------------------------------------------------------


def make_hello() -> list[Any]:
    return [WampMessageType.HELLO, "realm1", {"roles": {"callee": {}}}]


def make_goodbye() -> list[Any]:
    return [WampMessageType.GOODBYE, {}, "wamp.close.close_realm"]


def make_register(request_id: int, procedure: str) -> list[Any]:
    return [WampMessageType.REGISTER, request_id, {}, procedure]


def make_yield_msg(request_id: int, args: list[Any] | None = None) -> list[Any]:
    """Build a YIELD message."""
    msg: list[Any] = [WampMessageType.YIELD, request_id, {}]
    if args is not None:
        msg.append(args)
    return msg


def make_error_msg(
    request_type: int,
    request_id: int,
    error_uri: str,
    args: list[Any] | None = None,
    kwargs: dict[str, Any] | None = None,
) -> list[Any]:
    """Build an ERROR message."""
    msg: list[Any] = [WampMessageType.ERROR, request_type, request_id, {}, error_uri]
    if kwargs:
        msg.append(args or [])
        msg.append(kwargs)
    elif args:
        msg.append(args)
    return msg


# ---------------------------------------------------------------------------
# Tests: session.call() basic functionality
# ---------------------------------------------------------------------------


class TestSessionCallBasic:
    """session.call() sends INVOCATION and returns result from YIELD."""

    async def test_call_success_returns_result(self) -> None:
        """Successful call: client responds with YIELD containing result."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.add")))

        call_result: list[Any] = []

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            # Process REGISTER
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            # Server calls the client RPC
            # We need to simulate the client responding with YIELD
            # Start the call in a background task
            async def do_call() -> None:
                result = await session.call("com.example.add", args=[3, 4])
                call_result.append(result)

            call_task = asyncio.create_task(do_call())

            # Wait a moment for the INVOCATION to be sent
            await asyncio.sleep(0.01)

            # Parse the INVOCATION that was sent
            # Messages: WELCOME (0), REGISTERED (1), INVOCATION (2)
            invocation = parse_sent(ws, 2)
            assert invocation[0] == WampMessageType.INVOCATION
            inv_request_id = invocation[1]
            assert invocation[2] > 0  # registration_id
            assert invocation[3] == {}  # details

            # Client sends YIELD
            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, [7])))

            # Process the YIELD response
            yield_msg = await session.receive_message()
            await hub._handle_yield(session, yield_msg)

            await call_task

            # Goodbye
            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert len(call_result) == 1
        assert call_result[0] == 7

    async def test_call_with_kwargs(self) -> None:
        """Call with kwargs sends INVOCATION with kwargs in message."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.greet")))

        call_result: list[Any] = []

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            async def do_call() -> None:
                result = await session.call(
                    "com.example.greet",
                    args=["hello"],
                    kwargs={"name": "world"},
                )
                call_result.append(result)

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            # Verify INVOCATION has both args and kwargs
            invocation = parse_sent(ws, 2)
            assert invocation[0] == WampMessageType.INVOCATION
            inv_request_id = invocation[1]
            assert invocation[4] == ["hello"]  # args
            assert invocation[5] == {"name": "world"}  # kwargs

            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, ["hello world"])))
            yield_msg = await session.receive_message()
            await hub._handle_yield(session, yield_msg)
            await call_task

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert call_result[0] == "hello world"

    async def test_call_with_args_only(self) -> None:
        """Call with args but no kwargs sends INVOCATION with only args."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.add")))

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            async def do_call() -> None:
                await session.call("com.example.add", args=[1, 2])

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            invocation = parse_sent(ws, 2)
            assert invocation[0] == WampMessageType.INVOCATION
            inv_request_id = invocation[1]
            # Should have args but no kwargs
            assert invocation[4] == [1, 2]
            assert len(invocation) == 5  # no kwargs element

            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, [3])))
            yield_msg = await session.receive_message()
            await hub._handle_yield(session, yield_msg)
            await call_task

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

    async def test_call_no_args(self) -> None:
        """Call with no args or kwargs sends minimal INVOCATION."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.ping")))

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            async def do_call() -> None:
                await session.call("com.example.ping")

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            invocation = parse_sent(ws, 2)
            assert invocation[0] == WampMessageType.INVOCATION
            inv_request_id = invocation[1]
            # Minimal message: [INVOCATION, req_id, reg_id, details]
            assert len(invocation) == 4

            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id)))
            yield_msg = await session.receive_message()
            await hub._handle_yield(session, yield_msg)
            await call_task

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

    async def test_call_none_result(self) -> None:
        """YIELD with empty args returns None."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.noop")))

        call_result: list[Any] = []

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            async def do_call() -> None:
                result = await session.call("com.example.noop")
                call_result.append(result)

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            invocation = parse_sent(ws, 2)
            inv_request_id = invocation[1]

            # YIELD with no args
            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id)))
            yield_msg = await session.receive_message()
            await hub._handle_yield(session, yield_msg)
            await call_task

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert call_result[0] is None

    async def test_call_multiple_args_in_yield(self) -> None:
        """YIELD with multiple args returns the full args list."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.multi")))

        call_result: list[Any] = []

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            async def do_call() -> None:
                result = await session.call("com.example.multi")
                call_result.append(result)

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            invocation = parse_sent(ws, 2)
            inv_request_id = invocation[1]

            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, [1, 2, 3])))
            yield_msg = await session.receive_message()
            await hub._handle_yield(session, yield_msg)
            await call_task

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert call_result[0] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Tests: Client error response
# ---------------------------------------------------------------------------


class TestSessionCallError:
    """Client responds with ERROR -> WampError raised."""

    async def test_call_client_error_response(self) -> None:
        """Client responds with ERROR -> session.call() raises WampError."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.fail")))

        call_error: list[Exception] = []

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            async def do_call() -> None:
                try:
                    await session.call("com.example.fail")
                except WampError as e:
                    call_error.append(e)

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            invocation = parse_sent(ws, 2)
            inv_request_id = invocation[1]

            # Client sends ERROR for the INVOCATION
            error_msg = make_error_msg(
                WampMessageType.INVOCATION,
                inv_request_id,
                WAMP_ERROR_RUNTIME_ERROR,
                args=["Something went wrong"],
            )
            ws.enqueue_text(json.dumps(error_msg))
            err = await session.receive_message()
            await hub._handle_invocation_error(session, err)
            await call_task

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert len(call_error) == 1
        exc = call_error[0]
        assert isinstance(exc, WampError)
        assert exc.uri == WAMP_ERROR_RUNTIME_ERROR

    async def test_call_client_error_with_kwargs(self) -> None:
        """Client error with kwargs is available on the exception."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.fail")))

        call_error: list[Exception] = []

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            async def do_call() -> None:
                try:
                    await session.call("com.example.fail")
                except WampError as e:
                    call_error.append(e)

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            invocation = parse_sent(ws, 2)
            inv_request_id = invocation[1]

            error_msg = make_error_msg(
                WampMessageType.INVOCATION,
                inv_request_id,
                WAMP_ERROR_RUNTIME_ERROR,
                args=["error occurred"],
                kwargs={"code": 42},
            )
            ws.enqueue_text(json.dumps(error_msg))
            err = await session.receive_message()
            await hub._handle_invocation_error(session, err)
            await call_task

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert len(call_error) == 1
        exc = call_error[0]
        assert isinstance(exc, WampError)
        assert exc.wamp_args == ["error occurred"]
        assert exc.wamp_kwargs == {"code": 42}


# ---------------------------------------------------------------------------
# Tests: Timeout
# ---------------------------------------------------------------------------


class TestSessionCallTimeout:
    """Timeout raises WampCallTimeoutError."""

    async def test_call_timeout(self) -> None:
        """Call with timeout that expires raises WampCallTimeoutError."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.slow")))

        call_error: list[Exception] = []

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            async def do_call() -> None:
                try:
                    await session.call("com.example.slow", timeout=0.05)
                except WampCallTimeoutError as e:
                    call_error.append(e)

            call_task = asyncio.create_task(do_call())
            # Don't respond — let the timeout expire
            await call_task

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert len(call_error) == 1
        assert isinstance(call_error[0], WampCallTimeoutError)

    async def test_call_timeout_cleans_up_pending(self) -> None:
        """After timeout, the pending call is removed from the map."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.slow")))

        original_loop = hub._message_loop
        pending_count_after: list[int] = []

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            try:
                await session.call("com.example.slow", timeout=0.05)
            except WampCallTimeoutError:
                pass

            pending_count_after.append(len(session.pending_calls))

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert pending_count_after[0] == 0


# ---------------------------------------------------------------------------
# Tests: Non-existent procedure
# ---------------------------------------------------------------------------


class TestSessionCallNoSuchProcedure:
    """Calling a non-existent URI raises WampNoSuchProcedureError."""

    async def test_call_nonexistent_procedure(self) -> None:
        """session.call() for unknown URI raises WampNoSuchProcedureError."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        # Don't register anything

        call_error: list[Exception] = []

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            try:
                await session.call("com.example.unknown")
            except WampNoSuchProcedureError as e:
                call_error.append(e)

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert len(call_error) == 1
        assert isinstance(call_error[0], WampNoSuchProcedureError)

    async def test_call_nonexistent_sends_no_invocation(self) -> None:
        """session.call() for unknown URI does not send INVOCATION."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            try:
                await session.call("com.example.unknown")
            except WampNoSuchProcedureError:
                pass

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Only WELCOME + GOODBYE reply, no INVOCATION
        assert len(ws.sent_texts) == 2


# ---------------------------------------------------------------------------
# Tests: Disconnect during pending call
# ---------------------------------------------------------------------------


class TestSessionCallDisconnect:
    """Pending call futures rejected on session disconnect."""

    async def test_disconnect_rejects_pending_call(self) -> None:
        """When session disconnects, pending call futures are rejected with WampError."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.slow")))

        call_error: list[Exception] = []

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            from starlette.websockets import WebSocketDisconnect

            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            async def do_call() -> None:
                try:
                    await session.call("com.example.slow")
                except WampError as e:
                    call_error.append(e)

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            # Simulate disconnect by raising WebSocketDisconnect
            raise WebSocketDisconnect()

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Give the event loop a chance to propagate
        await asyncio.sleep(0.05)

        assert len(call_error) == 1
        assert isinstance(call_error[0], WampError)


# ---------------------------------------------------------------------------
# Tests: Server-initiated request IDs
# ---------------------------------------------------------------------------


class TestServerRequestIds:
    """Server-initiated request IDs use a separate counter."""

    async def test_request_ids_are_sequential(self) -> None:
        """Multiple session.call() uses sequential request IDs."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.add")))

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            # Make two calls
            async def do_call(idx: int) -> None:
                await session.call("com.example.add", args=[idx])

            call_task1 = asyncio.create_task(do_call(1))
            await asyncio.sleep(0.01)
            call_task2 = asyncio.create_task(do_call(2))
            await asyncio.sleep(0.01)

            # Parse the two INVOCATIONs
            inv1 = parse_sent(ws, 2)
            inv2 = parse_sent(ws, 3)
            assert inv1[0] == WampMessageType.INVOCATION
            assert inv2[0] == WampMessageType.INVOCATION

            req_id1 = inv1[1]
            req_id2 = inv2[1]

            # Sequential IDs
            assert req_id2 == req_id1 + 1

            # Respond to both
            ws.enqueue_text(json.dumps(make_yield_msg(req_id1, [10])))
            yield1 = await session.receive_message()
            await hub._handle_yield(session, yield1)

            ws.enqueue_text(json.dumps(make_yield_msg(req_id2, [20])))
            yield2 = await session.receive_message()
            await hub._handle_yield(session, yield2)

            await call_task1
            await call_task2

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tests: YIELD/ERROR routed via message dispatch loop
# ---------------------------------------------------------------------------


class TestMessageDispatchLoop:
    """YIELD and ERROR messages are routed correctly in the hub dispatch loop."""

    async def test_yield_via_dispatch_loop(self) -> None:
        """YIELD from client in the normal dispatch loop resolves pending call."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.add")))

        call_result: list[Any] = []

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            # Start a call in the background — it will send INVOCATION
            # and the client will respond via the dispatch loop

            async def do_call() -> None:
                result = await session.call("com.example.add", args=[5, 6])
                call_result.append(result)

            asyncio.create_task(do_call())

        # We need the client to respond with YIELD after receiving INVOCATION
        # Use a thin wrapper to inject the YIELD after some delay
        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            # Process REGISTER first
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            # Wait for INVOCATION to be sent
            await asyncio.sleep(0.05)

            # Parse the INVOCATION
            invocation = parse_sent(ws, 2)
            inv_request_id = invocation[1]

            # Enqueue YIELD and GOODBYE for the dispatch loop
            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, [11])))
            ws.enqueue_text(json.dumps(make_goodbye()))

            # Now run the original loop which will dispatch YIELD and GOODBYE
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Allow task to complete
        await asyncio.sleep(0.05)

        assert len(call_result) == 1
        assert call_result[0] == 11

    async def test_error_for_invocation_via_dispatch_loop(self) -> None:
        """ERROR for INVOCATION via dispatch loop rejects pending call."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.fail")))

        call_error: list[Exception] = []

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            async def do_call() -> None:
                try:
                    await session.call("com.example.fail")
                except WampError as e:
                    call_error.append(e)

            asyncio.create_task(do_call())

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            await asyncio.sleep(0.05)

            invocation = parse_sent(ws, 2)
            inv_request_id = invocation[1]

            error_msg = make_error_msg(
                WampMessageType.INVOCATION,
                inv_request_id,
                WAMP_ERROR_RUNTIME_ERROR,
                args=["failure"],
            )
            ws.enqueue_text(json.dumps(error_msg))
            ws.enqueue_text(json.dumps(make_goodbye()))

            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        await asyncio.sleep(0.05)

        assert len(call_error) == 1
        assert isinstance(call_error[0], WampError)
        assert call_error[0].uri == WAMP_ERROR_RUNTIME_ERROR


# ---------------------------------------------------------------------------
# Tests: FastAPI integration
# ---------------------------------------------------------------------------


class TestFastAPIIntegrationCallClient:
    """Integration tests using FastAPI TestClient for session.call()."""

    def _make_app(self, hub: WampHub) -> FastAPI:
        app = FastAPI()

        @app.websocket("/ws")
        async def wamp_endpoint(websocket: FastAPIWebSocket) -> None:
            await hub.handle_websocket(websocket)

        return app

    def test_call_client_rpc_via_fastapi(self) -> None:
        """Full flow: server calls client RPC via FastAPI test client.

        Uses a background task started from on_session_open that waits for
        the client to register a procedure then calls it.  The synchronous
        test-client side registers, receives the INVOCATION, and sends YIELD.
        """
        hub = WampHub(realm="realm1")

        call_result: list[Any] = []

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            async def do_call() -> None:
                # Poll until the client registers
                for _ in range(100):
                    if "com.client.echo" in session.client_rpc_uris:
                        break
                    await asyncio.sleep(0.01)

                try:
                    result = await session.call("com.client.echo", args=["ping"])
                    call_result.append(result)
                except Exception as e:
                    call_result.append(e)

            asyncio.create_task(do_call())

        app = self._make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                ws.send_json(make_hello())
                welcome: list[Any] = ws.receive_json()
                assert welcome[0] == WampMessageType.WELCOME

                # Client registers a procedure
                ws.send_json(make_register(1, "com.client.echo"))
                registered: list[Any] = ws.receive_json()
                assert registered[0] == WampMessageType.REGISTERED

                # Now expect the server to send INVOCATION
                invocation: list[Any] = ws.receive_json()
                assert invocation[0] == WampMessageType.INVOCATION
                inv_request_id = invocation[1]
                assert invocation[4] == ["ping"]  # args

                # Client responds with YIELD
                ws.send_json(make_yield_msg(inv_request_id, ["pong"]))

                # Small delay then close
                import time

                time.sleep(0.1)

                ws.send_json(make_goodbye())
                goodbye_reply: list[Any] = ws.receive_json()
                assert goodbye_reply[0] == WampMessageType.GOODBYE

        assert len(call_result) == 1
        assert call_result[0] == "pong"
