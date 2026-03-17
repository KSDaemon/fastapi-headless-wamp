"""Tests for call cancellation — client cancels server call (US-015).

Covers:
- Client sends CANCEL for a pending CALL → server cancels handler task
- Server sends ERROR with wamp.error.canceled
- CANCEL after handler already completed → ignored
- Cancel modes: 'skip' (let handler run), 'kill' (cancel task and wait),
  'killnowait' (cancel task, send ERROR immediately)
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import FastAPI
from fastapi import WebSocket as FastAPIWebSocket
from starlette.testclient import TestClient

from fastapi_headless_wamp.hub import WampHub
from fastapi_headless_wamp.protocol import (
    WAMP_ERROR_CANCELED,
    WampMessageType,
)

# ---------------------------------------------------------------------------
# Mock WebSocket (consistent with other test files)
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
    """Parse the JSON message at *index* from ws.sent_texts."""
    result: list[Any] = json.loads(ws.sent_texts[index])
    return result


def _make_app(hub: WampHub) -> FastAPI:
    """Create a FastAPI app with a /ws endpoint wired to *hub*."""
    app = FastAPI()

    @app.websocket("/ws")
    async def wamp_endpoint(websocket: FastAPIWebSocket) -> None:
        await hub.handle_websocket(websocket)

    return app


# ---------------------------------------------------------------------------
# Tests: cancel before completion (kill mode — default)
# ---------------------------------------------------------------------------


class TestCancelKillMode:
    """Default 'kill' mode: cancel the task, handler catches CancelledError."""

    async def test_cancel_long_running_handler(self) -> None:
        """CANCEL with kill mode cancels the handler and sends ERROR."""
        hub = WampHub(realm="realm1")
        handler_started = asyncio.Event()
        handler_cancelled = asyncio.Event()

        @hub.register("com.slow")
        async def slow_handler() -> str:
            handler_started.set()
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                handler_cancelled.set()
                raise
            return "done"

        ws = MockWebSocket(subprotocols=["wamp.2.json"])
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        ws.enqueue_text(json.dumps(hello))

        # Start the hub handler
        task = asyncio.create_task(hub.handle_websocket(ws))  # type: ignore[arg-type]

        # Wait for WELCOME
        await asyncio.sleep(0.05)

        # Send CALL
        call_msg: list[Any] = [WampMessageType.CALL, 1, {}, "com.slow"]
        ws.enqueue_text(json.dumps(call_msg))

        # Wait for handler to start
        await handler_started.wait()

        # Send CANCEL (default kill mode)
        cancel_msg: list[Any] = [WampMessageType.CANCEL, 1, {}]
        ws.enqueue_text(json.dumps(cancel_msg))

        # Wait for handler to be cancelled
        await asyncio.wait_for(handler_cancelled.wait(), timeout=2.0)

        # Give time for ERROR to be sent
        await asyncio.sleep(0.05)

        # Send GOODBYE to terminate
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(goodbye))

        await asyncio.wait_for(task, timeout=2.0)

        # Find the ERROR message (skip WELCOME at index 0)
        error_found = False
        for text in ws.sent_texts:
            msg = json.loads(text)
            if msg[0] == WampMessageType.ERROR:
                assert msg[1] == WampMessageType.CALL
                assert msg[2] == 1  # request_id
                assert msg[4] == WAMP_ERROR_CANCELED
                error_found = True
                break
        assert error_found, f"No ERROR message found in: {ws.sent_texts}"

    async def test_cancel_explicit_kill_mode(self) -> None:
        """CANCEL with explicit mode='kill' behaves the same as default."""
        hub = WampHub(realm="realm1")
        handler_started = asyncio.Event()

        @hub.register("com.slow")
        async def slow_handler() -> str:
            handler_started.set()
            await asyncio.sleep(100)
            return "done"

        ws = MockWebSocket(subprotocols=["wamp.2.json"])
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        ws.enqueue_text(json.dumps(hello))

        task = asyncio.create_task(hub.handle_websocket(ws))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)

        call_msg: list[Any] = [WampMessageType.CALL, 1, {}, "com.slow"]
        ws.enqueue_text(json.dumps(call_msg))
        await handler_started.wait()

        # Explicit kill mode
        cancel_msg: list[Any] = [WampMessageType.CANCEL, 1, {"mode": "kill"}]
        ws.enqueue_text(json.dumps(cancel_msg))

        await asyncio.sleep(0.1)

        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(goodbye))
        await asyncio.wait_for(task, timeout=2.0)

        # Verify ERROR was sent
        error_msgs = [json.loads(t) for t in ws.sent_texts if json.loads(t)[0] == WampMessageType.ERROR]
        assert len(error_msgs) == 1
        assert error_msgs[0][4] == WAMP_ERROR_CANCELED


# ---------------------------------------------------------------------------
# Tests: cancel after completion (ignored)
# ---------------------------------------------------------------------------


class TestCancelAfterCompletion:
    """CANCEL after the handler has already completed → ignored."""

    async def test_cancel_after_completion_ignored(self) -> None:
        """CANCEL for a request that already completed is silently ignored."""
        hub = WampHub(realm="realm1")

        @hub.register("com.fast")
        async def fast_handler() -> str:
            return "quick"

        ws = MockWebSocket(subprotocols=["wamp.2.json"])
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        ws.enqueue_text(json.dumps(hello))

        task = asyncio.create_task(hub.handle_websocket(ws))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)

        # Send CALL — handler completes immediately
        call_msg: list[Any] = [WampMessageType.CALL, 1, {}, "com.fast"]
        ws.enqueue_text(json.dumps(call_msg))

        # Give time for handler to complete and RESULT to be sent
        await asyncio.sleep(0.1)

        # Send CANCEL for the already-completed request
        cancel_msg: list[Any] = [WampMessageType.CANCEL, 1, {}]
        ws.enqueue_text(json.dumps(cancel_msg))

        await asyncio.sleep(0.05)

        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(goodbye))
        await asyncio.wait_for(task, timeout=2.0)

        # Only a RESULT should have been sent (no ERROR)
        result_msgs = [json.loads(t) for t in ws.sent_texts if json.loads(t)[0] == WampMessageType.RESULT]
        error_msgs = [json.loads(t) for t in ws.sent_texts if json.loads(t)[0] == WampMessageType.ERROR]

        assert len(result_msgs) == 1
        assert result_msgs[0][1] == 1  # request_id
        assert result_msgs[0][3] == ["quick"]
        assert len(error_msgs) == 0

    async def test_cancel_for_nonexistent_request_ignored(self) -> None:
        """CANCEL for a request_id that never existed → silently ignored."""
        hub = WampHub(realm="realm1")

        ws = MockWebSocket(subprotocols=["wamp.2.json"])
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        ws.enqueue_text(json.dumps(hello))

        task = asyncio.create_task(hub.handle_websocket(ws))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)

        # Send CANCEL for a non-existent request
        cancel_msg: list[Any] = [WampMessageType.CANCEL, 999, {}]
        ws.enqueue_text(json.dumps(cancel_msg))

        await asyncio.sleep(0.05)

        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(goodbye))
        await asyncio.wait_for(task, timeout=2.0)

        # No ERROR sent (only WELCOME + GOODBYE reply)
        error_msgs = [json.loads(t) for t in ws.sent_texts if json.loads(t)[0] == WampMessageType.ERROR]
        assert len(error_msgs) == 0


# ---------------------------------------------------------------------------
# Tests: cancel modes
# ---------------------------------------------------------------------------


class TestCancelSkipMode:
    """'skip' mode: stop waiting for result, let handler continue."""

    async def test_skip_mode_sends_error_immediately(self) -> None:
        """CANCEL(skip) sends ERROR immediately; handler keeps running."""
        hub = WampHub(realm="realm1")
        handler_started = asyncio.Event()
        handler_finished = asyncio.Event()

        @hub.register("com.slow")
        async def slow_handler() -> str:
            handler_started.set()
            await asyncio.sleep(0.3)
            handler_finished.set()
            return "done"

        ws = MockWebSocket(subprotocols=["wamp.2.json"])
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        ws.enqueue_text(json.dumps(hello))

        task = asyncio.create_task(hub.handle_websocket(ws))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)

        call_msg: list[Any] = [WampMessageType.CALL, 1, {}, "com.slow"]
        ws.enqueue_text(json.dumps(call_msg))
        await handler_started.wait()

        # CANCEL with skip mode
        cancel_msg: list[Any] = [WampMessageType.CANCEL, 1, {"mode": "skip"}]
        ws.enqueue_text(json.dumps(cancel_msg))

        await asyncio.sleep(0.05)

        # ERROR should be sent immediately
        error_msgs = [json.loads(t) for t in ws.sent_texts if json.loads(t)[0] == WampMessageType.ERROR]
        assert len(error_msgs) == 1
        assert error_msgs[0][4] == WAMP_ERROR_CANCELED

        # Handler should still be running
        assert not handler_finished.is_set()

        # Wait for handler to finish
        await asyncio.wait_for(handler_finished.wait(), timeout=2.0)

        # No duplicate RESULT should be sent (suppressed by cancelled marker)
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(goodbye))
        await asyncio.wait_for(task, timeout=2.0)

        # Only 1 ERROR, no RESULT
        result_msgs = [json.loads(t) for t in ws.sent_texts if json.loads(t)[0] == WampMessageType.RESULT]
        all_error_msgs = [json.loads(t) for t in ws.sent_texts if json.loads(t)[0] == WampMessageType.ERROR]
        assert len(result_msgs) == 0
        assert len(all_error_msgs) == 1


class TestCancelKillNoWaitMode:
    """'killnowait' mode: cancel task, send ERROR immediately."""

    async def test_killnowait_sends_error_immediately(self) -> None:
        """CANCEL(killnowait) cancels task and sends ERROR right away."""
        hub = WampHub(realm="realm1")
        handler_started = asyncio.Event()

        @hub.register("com.slow")
        async def slow_handler() -> str:
            handler_started.set()
            await asyncio.sleep(100)
            return "done"

        ws = MockWebSocket(subprotocols=["wamp.2.json"])
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        ws.enqueue_text(json.dumps(hello))

        task = asyncio.create_task(hub.handle_websocket(ws))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)

        call_msg: list[Any] = [WampMessageType.CALL, 1, {}, "com.slow"]
        ws.enqueue_text(json.dumps(call_msg))
        await handler_started.wait()

        # CANCEL with killnowait mode
        cancel_msg: list[Any] = [WampMessageType.CANCEL, 1, {"mode": "killnowait"}]
        ws.enqueue_text(json.dumps(cancel_msg))

        await asyncio.sleep(0.1)

        # ERROR should have been sent
        error_msgs = [json.loads(t) for t in ws.sent_texts if json.loads(t)[0] == WampMessageType.ERROR]
        assert len(error_msgs) == 1
        assert error_msgs[0][4] == WAMP_ERROR_CANCELED
        assert error_msgs[0][2] == 1  # request_id

        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(goodbye))
        await asyncio.wait_for(task, timeout=2.0)

        # No duplicate ERROR (the task's CancelledError should be suppressed)
        all_error_msgs = [json.loads(t) for t in ws.sent_texts if json.loads(t)[0] == WampMessageType.ERROR]
        assert len(all_error_msgs) == 1

    async def test_killnowait_no_duplicate_error(self) -> None:
        """killnowait doesn't send duplicate ERROR from the task's CancelledError."""
        hub = WampHub(realm="realm1")
        handler_started = asyncio.Event()

        @hub.register("com.slow")
        async def slow_handler() -> str:
            handler_started.set()
            await asyncio.sleep(100)
            return "done"

        ws = MockWebSocket(subprotocols=["wamp.2.json"])
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        ws.enqueue_text(json.dumps(hello))

        task = asyncio.create_task(hub.handle_websocket(ws))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)

        call_msg: list[Any] = [WampMessageType.CALL, 1, {}, "com.slow"]
        ws.enqueue_text(json.dumps(call_msg))
        await handler_started.wait()

        cancel_msg: list[Any] = [WampMessageType.CANCEL, 1, {"mode": "killnowait"}]
        ws.enqueue_text(json.dumps(cancel_msg))

        # Wait for everything to settle
        await asyncio.sleep(0.2)

        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(goodbye))
        await asyncio.wait_for(task, timeout=2.0)

        # Count all ERROR messages — should be exactly 1
        all_error_msgs = [json.loads(t) for t in ws.sent_texts if json.loads(t)[0] == WampMessageType.ERROR]
        assert len(all_error_msgs) == 1


# ---------------------------------------------------------------------------
# Tests: FastAPI integration
# ---------------------------------------------------------------------------


class TestCancelFastAPIIntegration:
    """End-to-end cancellation with FastAPI test client."""

    def test_cancel_via_fastapi_client(self) -> None:
        """Full cancellation flow via FastAPI WebSocket test client."""
        hub = WampHub(realm="realm1")
        handler_started = asyncio.Event()

        @hub.register("com.slow")
        async def slow_handler() -> str:
            handler_started.set()
            await asyncio.sleep(100)
            return "done"

        # Use on_session_open to send CALL + CANCEL from the server side
        # so we can verify in the synchronous test client.
        app = _make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                # Handshake
                hello: list[Any] = [
                    WampMessageType.HELLO,
                    "realm1",
                    {"roles": {"caller": {}, "callee": {}}},
                ]
                ws.send_json(hello)
                welcome: list[Any] = ws.receive_json()
                assert welcome[0] == WampMessageType.WELCOME

                # Send CALL
                call_msg: list[Any] = [WampMessageType.CALL, 1, {}, "com.slow"]
                ws.send_json(call_msg)

                # Small sleep to let handler start

                time.sleep(0.1)

                # Send CANCEL (kill mode)
                cancel_msg: list[Any] = [WampMessageType.CANCEL, 1, {"mode": "kill"}]
                ws.send_json(cancel_msg)

                # Receive ERROR
                error_msg: list[Any] = ws.receive_json()
                assert error_msg[0] == WampMessageType.ERROR
                assert error_msg[1] == WampMessageType.CALL
                assert error_msg[2] == 1
                assert error_msg[4] == WAMP_ERROR_CANCELED

                # Send GOODBYE
                goodbye: list[Any] = [
                    WampMessageType.GOODBYE,
                    {},
                    "wamp.close.normal",
                ]
                ws.send_json(goodbye)
                goodbye_reply: list[Any] = ws.receive_json()
                assert goodbye_reply[0] == WampMessageType.GOODBYE


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------


class TestCancelEdgeCases:
    """Edge cases and regression tests for CANCEL."""

    async def test_cancel_sync_handler(self) -> None:
        """CANCEL works for sync handlers running via asyncio.to_thread()."""
        hub = WampHub(realm="realm1")
        handler_started = asyncio.Event()

        @hub.register("com.sync_slow")
        def sync_slow_handler() -> str:
            # Signal start from thread
            handler_started._loop.call_soon_threadsafe(handler_started.set)  # type: ignore[attr-defined]

            time.sleep(10)
            return "done"

        ws = MockWebSocket(subprotocols=["wamp.2.json"])
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        ws.enqueue_text(json.dumps(hello))

        task = asyncio.create_task(hub.handle_websocket(ws))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)

        call_msg: list[Any] = [WampMessageType.CALL, 1, {}, "com.sync_slow"]
        ws.enqueue_text(json.dumps(call_msg))

        # Wait for handler to start
        await asyncio.wait_for(handler_started.wait(), timeout=2.0)

        # CANCEL with kill mode
        cancel_msg: list[Any] = [WampMessageType.CANCEL, 1, {"mode": "killnowait"}]
        ws.enqueue_text(json.dumps(cancel_msg))

        await asyncio.sleep(0.1)

        # ERROR should be sent
        error_msgs = [json.loads(t) for t in ws.sent_texts if json.loads(t)[0] == WampMessageType.ERROR]
        assert len(error_msgs) >= 1
        assert error_msgs[0][4] == WAMP_ERROR_CANCELED

        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(goodbye))
        await asyncio.wait_for(task, timeout=5.0)

    async def test_multiple_calls_cancel_one(self) -> None:
        """Cancelling one request doesn't affect another."""
        hub = WampHub(realm="realm1")
        handler1_started = asyncio.Event()
        handler2_result = asyncio.Event()

        @hub.register("com.slow")
        async def slow_handler() -> str:
            handler1_started.set()
            await asyncio.sleep(100)
            return "slow done"

        @hub.register("com.fast")
        async def fast_handler() -> str:
            handler2_result.set()
            return "fast done"

        ws = MockWebSocket(subprotocols=["wamp.2.json"])
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        ws.enqueue_text(json.dumps(hello))

        task = asyncio.create_task(hub.handle_websocket(ws))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)

        # Send two CALLs with different request IDs
        call1: list[Any] = [WampMessageType.CALL, 1, {}, "com.slow"]
        ws.enqueue_text(json.dumps(call1))
        await handler1_started.wait()

        call2: list[Any] = [WampMessageType.CALL, 2, {}, "com.fast"]
        ws.enqueue_text(json.dumps(call2))

        # Wait for fast handler to complete
        await asyncio.wait_for(handler2_result.wait(), timeout=2.0)
        await asyncio.sleep(0.05)

        # Cancel request 1 only
        cancel_msg: list[Any] = [WampMessageType.CANCEL, 1, {"mode": "kill"}]
        ws.enqueue_text(json.dumps(cancel_msg))

        await asyncio.sleep(0.1)

        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(goodbye))
        await asyncio.wait_for(task, timeout=2.0)

        # Check: RESULT for request 2 and ERROR for request 1
        result_msgs = [json.loads(t) for t in ws.sent_texts if json.loads(t)[0] == WampMessageType.RESULT]
        error_msgs = [json.loads(t) for t in ws.sent_texts if json.loads(t)[0] == WampMessageType.ERROR]

        # Fast handler should have RESULT
        assert any(m[1] == 2 and m[3] == ["fast done"] for m in result_msgs)

        # Slow handler should have ERROR (canceled)
        assert any(m[2] == 1 and m[4] == WAMP_ERROR_CANCELED for m in error_msgs)

    async def test_invalid_cancel_message_ignored(self) -> None:
        """Invalid CANCEL message (wrong structure) is logged and ignored."""
        hub = WampHub(realm="realm1")

        ws = MockWebSocket(subprotocols=["wamp.2.json"])
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        ws.enqueue_text(json.dumps(hello))

        task = asyncio.create_task(hub.handle_websocket(ws))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)

        # Invalid CANCEL: missing options dict
        bad_cancel: list[Any] = [WampMessageType.CANCEL, "not_an_int", {}]
        ws.enqueue_text(json.dumps(bad_cancel))

        await asyncio.sleep(0.05)

        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(goodbye))
        await asyncio.wait_for(task, timeout=2.0)

        # No ERROR sent for invalid CANCEL
        error_msgs = [json.loads(t) for t in ws.sent_texts if json.loads(t)[0] == WampMessageType.ERROR]
        assert len(error_msgs) == 0

    async def test_cancel_request_id_matching(self) -> None:
        """ERROR response matches the CANCEL'd request_id exactly."""
        hub = WampHub(realm="realm1")
        handler_started = asyncio.Event()

        @hub.register("com.slow")
        async def slow_handler() -> str:
            handler_started.set()
            await asyncio.sleep(100)
            return "done"

        ws = MockWebSocket(subprotocols=["wamp.2.json"])
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        ws.enqueue_text(json.dumps(hello))

        task = asyncio.create_task(hub.handle_websocket(ws))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)

        # Use a specific request ID
        call_msg: list[Any] = [WampMessageType.CALL, 42, {}, "com.slow"]
        ws.enqueue_text(json.dumps(call_msg))
        await handler_started.wait()

        cancel_msg: list[Any] = [WampMessageType.CANCEL, 42, {}]
        ws.enqueue_text(json.dumps(cancel_msg))

        await asyncio.sleep(0.1)

        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(goodbye))
        await asyncio.wait_for(task, timeout=2.0)

        error_msgs = [json.loads(t) for t in ws.sent_texts if json.loads(t)[0] == WampMessageType.ERROR]
        assert len(error_msgs) == 1
        assert error_msgs[0][2] == 42  # request_id matches
