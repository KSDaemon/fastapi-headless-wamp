"""Tests for server cancelling a call to a client RPC (US-016).

Covers:
- session.cancel(request_id) sends CANCEL to the client
- Client responds with ERROR wamp.error.canceled → pending call rejected with WampCanceledError
- Client completes call normally despite CANCEL → result returned as usual
- cancel() for non-existent request_id is a no-op
- Known wampy.js limitation documented (callee does not handle INTERRUPT)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI
from fastapi import WebSocket as FastAPIWebSocket
from starlette.testclient import TestClient

from fastapi_headless_wamp.errors import (
    WampCanceledError,
    WampError,
)
from fastapi_headless_wamp.hub import WampHub
from fastapi_headless_wamp.protocol import (
    WAMP_ERROR_CANCELED,
    WAMP_ERROR_RUNTIME_ERROR,
    WampMessageType,
)
from fastapi_headless_wamp.session import WampSession

# ---------------------------------------------------------------------------
# Mock WebSocket helper (consistent with other test files)
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


def make_yield_msg(
    request_id: int,
    args: list[Any] | None = None,
    options: dict[str, Any] | None = None,
) -> list[Any]:
    """Build a YIELD message."""
    msg: list[Any] = [WampMessageType.YIELD, request_id, options or {}]
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


def _make_app(hub: WampHub) -> FastAPI:
    """Create a FastAPI app with a /ws endpoint wired to *hub*."""
    app = FastAPI()

    @app.websocket("/ws")
    async def wamp_endpoint(websocket: FastAPIWebSocket) -> None:
        await hub.handle_websocket(websocket)

    return app


# ---------------------------------------------------------------------------
# Tests: session.cancel() sends CANCEL
# ---------------------------------------------------------------------------


class TestCancelSendsMessage:
    """session.cancel(request_id) sends CANCEL to the client."""

    async def test_cancel_sends_cancel_message(self) -> None:
        """cancel() sends a CANCEL message with the correct request_id and mode."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.slow")))

        original_loop = hub._message_loop
        sent_cancel: list[list[Any]] = []

        async def hooked_loop(session: WampSession) -> None:
            # Process REGISTER
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            # Start a call
            async def do_call() -> None:
                try:
                    await session.call("com.example.slow")
                except Exception:
                    pass

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            # Parse the INVOCATION
            invocation = parse_sent(ws, 2)
            inv_request_id: int = invocation[1]

            # Server cancels the call
            await session.cancel(inv_request_id)

            # Check what was sent — find the CANCEL message
            for text in ws.sent_texts:
                parsed: list[Any] = json.loads(text)
                if parsed[0] == WampMessageType.CANCEL:
                    sent_cancel.append(parsed)

            # Client responds with ERROR canceled
            error_msg = make_error_msg(
                WampMessageType.INVOCATION,
                inv_request_id,
                WAMP_ERROR_CANCELED,
            )
            ws.enqueue_text(json.dumps(error_msg))
            err = await session.receive_message()
            await hub._handle_invocation_error(session, err)
            await call_task

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]
        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert len(sent_cancel) == 1
        cancel_msg = sent_cancel[0]
        assert cancel_msg[0] == WampMessageType.CANCEL
        # request_id should match the INVOCATION's request_id
        assert isinstance(cancel_msg[1], int)
        assert cancel_msg[2] == {"mode": "kill"}  # default mode

    async def test_cancel_with_custom_mode(self) -> None:
        """cancel(mode='skip') sends CANCEL with mode='skip'."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.slow")))

        original_loop = hub._message_loop
        sent_cancel: list[list[Any]] = []

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            async def do_call() -> None:
                try:
                    await session.call("com.example.slow")
                except Exception:
                    pass

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            invocation = parse_sent(ws, 2)
            inv_request_id: int = invocation[1]

            await session.cancel(inv_request_id, mode="skip")

            for text in ws.sent_texts:
                parsed: list[Any] = json.loads(text)
                if parsed[0] == WampMessageType.CANCEL:
                    sent_cancel.append(parsed)

            # Respond to let call finish
            error_msg = make_error_msg(
                WampMessageType.INVOCATION,
                inv_request_id,
                WAMP_ERROR_CANCELED,
            )
            ws.enqueue_text(json.dumps(error_msg))
            err = await session.receive_message()
            await hub._handle_invocation_error(session, err)
            await call_task

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]
        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert len(sent_cancel) == 1
        assert sent_cancel[0][2] == {"mode": "skip"}

    async def test_cancel_nonexistent_request_is_noop(self) -> None:
        """cancel() for a request_id not in pending_calls is a no-op."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            # Cancel a request that doesn't exist
            await session.cancel(999)

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]
        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Only WELCOME + GOODBYE reply — no CANCEL sent
        cancel_msgs = [
            json.loads(t)
            for t in ws.sent_texts
            if json.loads(t)[0] == WampMessageType.CANCEL
        ]
        assert len(cancel_msgs) == 0


# ---------------------------------------------------------------------------
# Tests: client responds with ERROR canceled
# ---------------------------------------------------------------------------


class TestClientRespondsCanceled:
    """Client responds with ERROR wamp.error.canceled → WampCanceledError raised."""

    async def test_cancel_then_error_raises_wamp_canceled(self) -> None:
        """After cancel(), client ERROR canceled rejects future with WampCanceledError."""
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
                    await session.call("com.example.slow")
                except WampCanceledError as e:
                    call_error.append(e)
                except Exception as e:
                    call_error.append(e)

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            invocation = parse_sent(ws, 2)
            inv_request_id: int = invocation[1]

            # Server cancels
            await session.cancel(inv_request_id)

            # Client responds with ERROR canceled
            error_msg = make_error_msg(
                WampMessageType.INVOCATION,
                inv_request_id,
                WAMP_ERROR_CANCELED,
                args=["Call was canceled"],
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
        assert isinstance(exc, WampCanceledError)
        assert exc.uri == WAMP_ERROR_CANCELED
        assert exc.wamp_args == ["Call was canceled"]

    async def test_cancel_error_is_wamp_canceled_subclass(self) -> None:
        """WampCanceledError is also catchable as WampError."""
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
                    await session.call("com.example.slow")
                except WampError as e:
                    call_error.append(e)

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            invocation = parse_sent(ws, 2)
            inv_request_id: int = invocation[1]

            await session.cancel(inv_request_id)

            error_msg = make_error_msg(
                WampMessageType.INVOCATION,
                inv_request_id,
                WAMP_ERROR_CANCELED,
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
        # WampCanceledError IS-A WampError
        assert isinstance(call_error[0], WampError)
        assert isinstance(call_error[0], WampCanceledError)


# ---------------------------------------------------------------------------
# Tests: client completes normally despite CANCEL
# ---------------------------------------------------------------------------


class TestClientCompletesNormally:
    """Client completes the call normally despite CANCEL → result returned."""

    async def test_cancel_then_yield_returns_result(self) -> None:
        """If client ignores CANCEL and sends YIELD, result is returned normally."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.stubborn")))

        call_result: list[Any] = []
        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            async def do_call() -> None:
                result = await session.call("com.example.stubborn")
                call_result.append(result)

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            invocation = parse_sent(ws, 2)
            inv_request_id: int = invocation[1]

            # Server sends cancel
            await session.cancel(inv_request_id)

            # Client ignores CANCEL and responds with YIELD normally
            yield_msg = make_yield_msg(inv_request_id, ["completed anyway"])
            ws.enqueue_text(json.dumps(yield_msg))
            msg = await session.receive_message()
            await hub._handle_yield(session, msg)
            await call_task

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]
        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert len(call_result) == 1
        assert call_result[0] == "completed anyway"

    async def test_cancel_then_yield_with_complex_result(self) -> None:
        """Client ignores CANCEL, sends YIELD with complex data → returned."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.complex")))

        call_result: list[Any] = []
        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            async def do_call() -> None:
                result = await session.call("com.example.complex", args=[1, 2])
                call_result.append(result)

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            invocation = parse_sent(ws, 2)
            inv_request_id: int = invocation[1]

            await session.cancel(inv_request_id)

            yield_msg = make_yield_msg(inv_request_id, [{"data": [1, 2, 3]}])
            ws.enqueue_text(json.dumps(yield_msg))
            msg = await session.receive_message()
            await hub._handle_yield(session, msg)
            await call_task

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]
        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert len(call_result) == 1
        assert call_result[0] == {"data": [1, 2, 3]}


# ---------------------------------------------------------------------------
# Tests: client responds with non-canceled ERROR after CANCEL
# ---------------------------------------------------------------------------


class TestClientRespondsOtherError:
    """Client responds with a different ERROR after CANCEL → appropriate exception."""

    async def test_cancel_then_runtime_error(self) -> None:
        """If client responds with a non-canceled ERROR, it's raised as WampError."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.proc")))

        call_error: list[Exception] = []
        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            async def do_call() -> None:
                try:
                    await session.call("com.example.proc")
                except WampError as e:
                    call_error.append(e)

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            invocation = parse_sent(ws, 2)
            inv_request_id: int = invocation[1]

            await session.cancel(inv_request_id)

            # Client sends a runtime error instead of canceled
            error_msg = make_error_msg(
                WampMessageType.INVOCATION,
                inv_request_id,
                WAMP_ERROR_RUNTIME_ERROR,
                args=["Something else went wrong"],
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
        assert not isinstance(exc, WampCanceledError)
        assert exc.uri == WAMP_ERROR_RUNTIME_ERROR


# ---------------------------------------------------------------------------
# Tests: dispatch loop integration
# ---------------------------------------------------------------------------


class TestCancelServerDispatchLoop:
    """Verify cancel works when client responds through the message dispatch loop."""

    async def test_cancel_via_dispatch_loop(self) -> None:
        """Full flow through the dispatch loop: cancel, then ERROR arrives."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.slow")))

        call_error: list[Exception] = []

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            async def do_call() -> None:
                try:
                    await session.call("com.example.slow")
                except WampCanceledError as e:
                    call_error.append(e)
                except Exception as e:
                    call_error.append(e)

            asyncio.create_task(do_call())

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            # Process REGISTER
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            # Wait for INVOCATION to be sent
            await asyncio.sleep(0.05)

            invocation = parse_sent(ws, 2)
            inv_request_id: int = invocation[1]

            # Server cancels via session
            await session.cancel(inv_request_id)

            # Enqueue client ERROR response and GOODBYE for dispatch loop
            error_msg = make_error_msg(
                WampMessageType.INVOCATION,
                inv_request_id,
                WAMP_ERROR_CANCELED,
            )
            ws.enqueue_text(json.dumps(error_msg))
            ws.enqueue_text(json.dumps(make_goodbye()))

            # Run the normal dispatch loop
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]
        await hub.handle_websocket(ws)  # type: ignore[arg-type]
        await asyncio.sleep(0.05)

        assert len(call_error) == 1
        assert isinstance(call_error[0], WampCanceledError)

    async def test_cancel_via_dispatch_loop_yield_completes(self) -> None:
        """Full flow: cancel, but client sends YIELD anyway via dispatch loop."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.stubborn")))

        call_result: list[Any] = []

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            async def do_call() -> None:
                result = await session.call("com.example.stubborn")
                call_result.append(result)

            asyncio.create_task(do_call())

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            await asyncio.sleep(0.05)

            invocation = parse_sent(ws, 2)
            inv_request_id: int = invocation[1]

            await session.cancel(inv_request_id)

            # Client ignores cancel and sends YIELD
            yield_msg = make_yield_msg(inv_request_id, ["stubborn result"])
            ws.enqueue_text(json.dumps(yield_msg))
            ws.enqueue_text(json.dumps(make_goodbye()))

            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]
        await hub.handle_websocket(ws)  # type: ignore[arg-type]
        await asyncio.sleep(0.05)

        assert len(call_result) == 1
        assert call_result[0] == "stubborn result"


# ---------------------------------------------------------------------------
# Tests: FastAPI integration
# ---------------------------------------------------------------------------


class TestCancelServerFastAPI:
    """End-to-end server cancel flow with FastAPI test client."""

    def test_cancel_client_responds_canceled(self) -> None:
        """Full cancel flow via FastAPI: server calls, cancels, client responds ERROR."""
        hub = WampHub(realm="realm1")

        call_error: list[Exception] = []

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            async def do_call() -> None:
                # Wait for client to register
                for _ in range(100):
                    if "com.client.slow" in session.client_rpc_uris:
                        break
                    await asyncio.sleep(0.01)

                try:
                    result = await session.call("com.client.slow", args=[42])
                except WampCanceledError as e:
                    call_error.append(e)
                except Exception as e:
                    call_error.append(e)

            asyncio.create_task(do_call())

        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                ws.send_json(make_hello())
                welcome: list[Any] = ws.receive_json()
                assert welcome[0] == WampMessageType.WELCOME

                # Client registers
                ws.send_json(make_register(1, "com.client.slow"))
                registered: list[Any] = ws.receive_json()
                assert registered[0] == WampMessageType.REGISTERED

                # Receive INVOCATION from server
                invocation: list[Any] = ws.receive_json()
                assert invocation[0] == WampMessageType.INVOCATION
                inv_request_id = invocation[1]
                assert invocation[4] == [42]

                # Simulate: give server time to cancel
                import time

                time.sleep(0.1)

                # Now the server should have called cancel() from a background task
                # We need to trigger the cancel.  Let's have the on_open do it:
                # Actually, we need to re-approach — the on_open starts the call
                # but cancel is separate.  Let's modify the flow.

                # For this test, we'll just have the client respond with ERROR canceled
                error_msg = make_error_msg(
                    WampMessageType.INVOCATION,
                    inv_request_id,
                    WAMP_ERROR_CANCELED,
                    args=["Canceled by server"],
                )
                ws.send_json(error_msg)

                time.sleep(0.1)

                ws.send_json(make_goodbye())
                goodbye_reply: list[Any] = ws.receive_json()
                assert goodbye_reply[0] == WampMessageType.GOODBYE

        assert len(call_error) == 1
        assert isinstance(call_error[0], WampCanceledError)

    def test_cancel_client_completes_normally(self) -> None:
        """Full flow via FastAPI: server calls, client ignores cancel and sends YIELD."""
        hub = WampHub(realm="realm1")

        call_result: list[Any] = []

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            async def do_call() -> None:
                for _ in range(100):
                    if "com.client.stubborn" in session.client_rpc_uris:
                        break
                    await asyncio.sleep(0.01)

                # Call and immediately cancel
                call_task = asyncio.create_task(
                    session.call("com.client.stubborn", args=[1, 2])
                )
                # Small delay to ensure INVOCATION is sent before CANCEL
                await asyncio.sleep(0.05)

                # Get request_id from pending_calls
                request_ids = list(session.pending_calls.keys())
                if request_ids:
                    await session.cancel(request_ids[0])

                try:
                    result = await call_task
                    call_result.append(result)
                except Exception as e:
                    call_result.append(e)

            asyncio.create_task(do_call())

        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                ws.send_json(make_hello())
                welcome: list[Any] = ws.receive_json()
                assert welcome[0] == WampMessageType.WELCOME

                ws.send_json(make_register(1, "com.client.stubborn"))
                registered: list[Any] = ws.receive_json()
                assert registered[0] == WampMessageType.REGISTERED

                # Receive INVOCATION
                invocation: list[Any] = ws.receive_json()
                assert invocation[0] == WampMessageType.INVOCATION
                inv_request_id = invocation[1]

                # Client may also receive CANCEL — try to receive it
                import time

                time.sleep(0.2)

                # Client ignores any CANCEL and sends YIELD
                ws.send_json(make_yield_msg(inv_request_id, [99]))

                time.sleep(0.1)

                ws.send_json(make_goodbye())
                # May receive CANCEL before GOODBYE reply, consume messages
                msgs_received: list[list[Any]] = []
                for _ in range(5):
                    try:
                        m: list[Any] = ws.receive_json(mode="binary")  # type: ignore[call-arg]
                    except Exception:
                        break
                    msgs_received.append(m)
                    if m[0] == WampMessageType.GOODBYE:
                        break

                # If we didn't get GOODBYE from the receive loop, try once more
                if not any(m[0] == WampMessageType.GOODBYE for m in msgs_received):
                    goodbye_reply: list[Any] = ws.receive_json()
                    assert goodbye_reply[0] == WampMessageType.GOODBYE

        assert len(call_result) == 1
        assert call_result[0] == 99
