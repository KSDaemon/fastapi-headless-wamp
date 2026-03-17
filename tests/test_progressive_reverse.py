"""Tests for progressive call results on server-to-client calls (US-013).

Covers:
- session.call(receive_progress=True, on_progress=callback) sends INVOCATION
  with details.receive_progress = true
- Progressive YIELD messages from client (with options.progress = true)
  trigger on_progress callback with the intermediate result
- Final YIELD (without progress flag) resolves the call future with the
  final result
- If on_progress is not provided but receive_progress is True, progressive
  yields are silently consumed
- Cleanup of progress callbacks on timeout and disconnect
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from fastapi import FastAPI
from fastapi import WebSocket as FastAPIWebSocket
from starlette.testclient import TestClient

from fastapi_headless_wamp.errors import (
    WampCallTimeoutError,
)
from fastapi_headless_wamp.hub import WampHub
from fastapi_headless_wamp.protocol import WampMessageType
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


def make_yield_msg(
    request_id: int,
    args: list[Any] | None = None,
    progress: bool = False,
) -> list[Any]:
    """Build a YIELD message, optionally with progress flag."""
    options: dict[str, Any] = {}
    if progress:
        options["progress"] = True
    msg: list[Any] = [WampMessageType.YIELD, request_id, options]
    if args is not None:
        msg.append(args)
    return msg


# ---------------------------------------------------------------------------
# Tests: Progressive YIELD triggers on_progress callback
# ---------------------------------------------------------------------------


class TestProgressiveYieldCallback:
    """Progressive YIELD messages trigger the on_progress callback."""

    async def test_progressive_yields_trigger_callback(self) -> None:
        """Multiple progressive YIELDs invoke on_progress, final resolves."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.stream")))

        call_result: list[Any] = []
        progress_values: list[Any] = []

        async def on_progress(value: Any) -> None:
            progress_values.append(value)

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            # Process REGISTER
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            # Server calls with progressive results
            async def do_call() -> None:
                result = await session.call(
                    "com.example.stream",
                    receive_progress=True,
                    on_progress=on_progress,
                )
                call_result.append(result)

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            # Verify INVOCATION has receive_progress in details
            invocation = parse_sent(ws, 2)
            assert invocation[0] == WampMessageType.INVOCATION
            inv_request_id = invocation[1]
            assert invocation[3].get("receive_progress") is True

            # Client sends progressive YIELDs
            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, ["chunk1"], progress=True)))
            yield1 = await session.receive_message()
            await hub._handle_yield(session, yield1)

            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, ["chunk2"], progress=True)))
            yield2 = await session.receive_message()
            await hub._handle_yield(session, yield2)

            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, ["chunk3"], progress=True)))
            yield3 = await session.receive_message()
            await hub._handle_yield(session, yield3)

            # Final YIELD (no progress flag)
            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, ["final"])))
            yield_final = await session.receive_message()
            await hub._handle_yield(session, yield_final)

            await call_task

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]
        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert progress_values == ["chunk1", "chunk2", "chunk3"]
        assert len(call_result) == 1
        assert call_result[0] == "final"

    async def test_single_progressive_yield(self) -> None:
        """Single progressive YIELD followed by final YIELD."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.stream")))

        call_result: list[Any] = []
        progress_values: list[Any] = []

        async def on_progress(value: Any) -> None:
            progress_values.append(value)

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            async def do_call() -> None:
                result = await session.call(
                    "com.example.stream",
                    receive_progress=True,
                    on_progress=on_progress,
                )
                call_result.append(result)

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            invocation = parse_sent(ws, 2)
            inv_request_id = invocation[1]

            # One progressive yield
            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, [42], progress=True)))
            yield1 = await session.receive_message()
            await hub._handle_yield(session, yield1)

            # Final
            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, [100])))
            yield_final = await session.receive_message()
            await hub._handle_yield(session, yield_final)

            await call_task

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]
        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert progress_values == [42]
        assert call_result[0] == 100

    async def test_progressive_yield_with_none_data(self) -> None:
        """Progressive YIELD with no args passes None to callback."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.stream")))

        call_result: list[Any] = []
        progress_values: list[Any] = []

        async def on_progress(value: Any) -> None:
            progress_values.append(value)

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            async def do_call() -> None:
                result = await session.call(
                    "com.example.stream",
                    receive_progress=True,
                    on_progress=on_progress,
                )
                call_result.append(result)

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            invocation = parse_sent(ws, 2)
            inv_request_id = invocation[1]

            # Progressive yield with no args
            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, progress=True)))
            yield1 = await session.receive_message()
            await hub._handle_yield(session, yield1)

            # Final
            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, ["done"])))
            yield_final = await session.receive_message()
            await hub._handle_yield(session, yield_final)

            await call_task

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]
        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert progress_values == [None]
        assert call_result[0] == "done"


# ---------------------------------------------------------------------------
# Tests: Final YIELD resolves the call
# ---------------------------------------------------------------------------


class TestFinalYieldResolves:
    """Final YIELD (without progress flag) resolves the future."""

    async def test_final_yield_resolves_after_progressive(self) -> None:
        """The final YIELD properly resolves the call future."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.stream")))

        call_result: list[Any] = []

        async def on_progress(value: Any) -> None:
            pass  # consume

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            async def do_call() -> None:
                result = await session.call(
                    "com.example.stream",
                    receive_progress=True,
                    on_progress=on_progress,
                )
                call_result.append(result)

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            invocation = parse_sent(ws, 2)
            inv_request_id = invocation[1]

            # Progressive
            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, ["p1"], progress=True)))
            y1 = await session.receive_message()
            await hub._handle_yield(session, y1)

            # Final YIELD with complex data
            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, [{"status": "complete", "count": 3}])))
            y_final = await session.receive_message()
            await hub._handle_yield(session, y_final)

            await call_task

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]
        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert call_result[0] == {"status": "complete", "count": 3}

    async def test_invocation_has_receive_progress_details(self) -> None:
        """INVOCATION message has details.receive_progress = true."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.stream")))

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            async def do_call() -> None:
                await session.call(
                    "com.example.stream",
                    receive_progress=True,
                )

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            # Verify INVOCATION details
            invocation = parse_sent(ws, 2)
            assert invocation[0] == WampMessageType.INVOCATION
            assert invocation[3]["receive_progress"] is True

            # Respond so the call completes
            inv_request_id = invocation[1]
            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, ["result"])))
            y = await session.receive_message()
            await hub._handle_yield(session, y)
            await call_task

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]
        await hub.handle_websocket(ws)  # type: ignore[arg-type]

    async def test_non_progressive_call_no_receive_progress_in_details(self) -> None:
        """Non-progressive call does not have receive_progress in details."""
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
            assert invocation[3] == {}  # no receive_progress

            inv_request_id = invocation[1]
            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, [3])))
            y = await session.receive_message()
            await hub._handle_yield(session, y)
            await call_task

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]
        await hub.handle_websocket(ws)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tests: No callback case (silently consumed)
# ---------------------------------------------------------------------------


class TestNoCallbackCase:
    """receive_progress=True without on_progress: progressive YIELDs consumed."""

    async def test_progressive_without_callback_silently_consumed(self) -> None:
        """Progressive YIELDs are silently consumed when no callback provided."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.stream")))

        call_result: list[Any] = []

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            async def do_call() -> None:
                # receive_progress=True but no on_progress callback
                result = await session.call(
                    "com.example.stream",
                    receive_progress=True,
                )
                call_result.append(result)

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            invocation = parse_sent(ws, 2)
            inv_request_id = invocation[1]

            # Send progressive yields — should be silently consumed
            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, ["chunk1"], progress=True)))
            y1 = await session.receive_message()
            await hub._handle_yield(session, y1)

            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, ["chunk2"], progress=True)))
            y2 = await session.receive_message()
            await hub._handle_yield(session, y2)

            # Final
            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, ["final"])))
            y_final = await session.receive_message()
            await hub._handle_yield(session, y_final)

            await call_task

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]
        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # No crash, final result returned correctly
        assert call_result[0] == "final"


# ---------------------------------------------------------------------------
# Tests: Cleanup of progress callbacks
# ---------------------------------------------------------------------------


class TestProgressCallbackCleanup:
    """Progress callbacks are cleaned up properly."""

    async def test_progress_callback_cleaned_up_after_completion(self) -> None:
        """After the call completes, progress callback is removed."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.stream")))

        progress_cb_count_after: list[int] = []

        async def on_progress(value: Any) -> None:
            pass

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    session.call(
                        "com.example.stream",
                        receive_progress=True,
                        on_progress=on_progress,
                    ),
                    timeout=2.0,
                )

            # Check that progress callbacks map is empty after call
            progress_cb_count_after.append(len(session._pending_progress_callbacks))

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        # We need the call to complete, so inject the YIELD into the loop
        orig_message_loop = hub._message_loop

        async def final_hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            async def do_call() -> None:
                await session.call(
                    "com.example.stream",
                    receive_progress=True,
                    on_progress=on_progress,
                )

            call_task = asyncio.create_task(do_call())
            await asyncio.sleep(0.01)

            invocation = parse_sent(ws, 2)
            inv_request_id = invocation[1]

            # Final yield
            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, ["done"])))
            y = await session.receive_message()
            await hub._handle_yield(session, y)
            await call_task

            # Check cleanup
            progress_cb_count_after.append(len(session._pending_progress_callbacks))

            ws.enqueue_text(json.dumps(make_goodbye()))
            await orig_message_loop(session)

        hub._message_loop = final_hooked_loop  # type: ignore[method-assign]
        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert progress_cb_count_after[0] == 0

    async def test_progress_callback_cleaned_up_on_timeout(self) -> None:
        """After timeout, progress callback is removed."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.slow")))

        progress_cb_count_after: list[int] = []

        async def on_progress(value: Any) -> None:
            pass

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            with contextlib.suppress(WampCallTimeoutError):
                await session.call(
                    "com.example.slow",
                    receive_progress=True,
                    on_progress=on_progress,
                    timeout=0.05,
                )

            progress_cb_count_after.append(len(session._pending_progress_callbacks))

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]
        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert progress_cb_count_after[0] == 0


# ---------------------------------------------------------------------------
# Tests: Via dispatch loop
# ---------------------------------------------------------------------------


class TestProgressiveViaDispatchLoop:
    """Progressive YIELDs handled through the hub message dispatch loop."""

    async def test_progressive_via_dispatch_loop(self) -> None:
        """Progressive + final YIELD via the normal hub dispatch loop."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.stream")))

        call_result: list[Any] = []
        progress_values: list[Any] = []

        async def on_progress(value: Any) -> None:
            progress_values.append(value)

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            async def do_call() -> None:
                result = await session.call(
                    "com.example.stream",
                    receive_progress=True,
                    on_progress=on_progress,
                )
                call_result.append(result)

            asyncio.create_task(do_call())

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            # Process REGISTER
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            # Wait for the INVOCATION to be sent
            await asyncio.sleep(0.05)

            invocation = parse_sent(ws, 2)
            inv_request_id = invocation[1]

            # Enqueue progressive + final YIELDs and GOODBYE for dispatch
            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, ["p1"], progress=True)))
            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, ["p2"], progress=True)))
            ws.enqueue_text(json.dumps(make_yield_msg(inv_request_id, ["final_result"])))
            ws.enqueue_text(json.dumps(make_goodbye()))

            # Run the original loop to dispatch everything
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]
        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        await asyncio.sleep(0.05)

        assert progress_values == ["p1", "p2"]
        assert len(call_result) == 1
        assert call_result[0] == "final_result"


# ---------------------------------------------------------------------------
# Tests: FastAPI integration
# ---------------------------------------------------------------------------


class TestFastAPIIntegrationProgressive:
    """Integration tests using FastAPI TestClient for progressive calls."""

    def _make_app(self, hub: WampHub) -> FastAPI:
        app = FastAPI()

        @app.websocket("/ws")
        async def wamp_endpoint(websocket: FastAPIWebSocket) -> None:
            await hub.handle_websocket(websocket)

        return app

    def test_progressive_call_via_fastapi(self) -> None:
        """Full flow: server calls client RPC with progressive results via FastAPI."""
        hub = WampHub(realm="realm1")

        call_result: list[Any] = []
        progress_values: list[Any] = []

        async def on_progress(value: Any) -> None:
            progress_values.append(value)

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            async def do_call() -> None:
                # Poll until the client registers
                for _ in range(100):
                    if "com.client.stream" in session.client_rpc_uris:
                        break
                    await asyncio.sleep(0.01)

                try:
                    result = await session.call(
                        "com.client.stream",
                        receive_progress=True,
                        on_progress=on_progress,
                    )
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
                ws.send_json(make_register(1, "com.client.stream"))
                registered: list[Any] = ws.receive_json()
                assert registered[0] == WampMessageType.REGISTERED

                # Now expect the server to send INVOCATION
                invocation: list[Any] = ws.receive_json()
                assert invocation[0] == WampMessageType.INVOCATION
                inv_request_id = invocation[1]
                assert invocation[3].get("receive_progress") is True

                # Client sends progressive YIELDs
                ws.send_json(make_yield_msg(inv_request_id, ["progress_1"], progress=True))
                ws.send_json(make_yield_msg(inv_request_id, ["progress_2"], progress=True))

                # Final YIELD
                ws.send_json(make_yield_msg(inv_request_id, ["all_done"]))

                # Small delay then close
                import time

                time.sleep(0.2)

                ws.send_json(make_goodbye())
                goodbye_reply: list[Any] = ws.receive_json()
                assert goodbye_reply[0] == WampMessageType.GOODBYE

        assert progress_values == ["progress_1", "progress_2"]
        assert len(call_result) == 1
        assert call_result[0] == "all_done"
