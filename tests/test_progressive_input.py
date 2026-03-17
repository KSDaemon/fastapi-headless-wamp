"""Tests for progressive call invocations (US-014).

Covers:
- Client sends multiple CALL messages with the same request_id and options.progress = true
- Server accumulates progressive input chunks and makes them available via async iterator
- Final CALL from client has options.progress absent or false, signaling end of input
- RPC handler iterates over input chunks as they arrive and produces result after all input
- Non-existent procedure returns error on first progressive CALL
- Handler receives all chunks in order
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi import WebSocket as FastAPIWebSocket
from starlette.testclient import TestClient

from fastapi_headless_wamp.hub import WampHub
from fastapi_headless_wamp.protocol import (
    WampMessageType,
)
from fastapi_headless_wamp.session import ProgressiveCallInput

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(hub: WampHub) -> FastAPI:
    """Create a FastAPI app with a /ws WAMP endpoint."""
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
    """Send GOODBYE, receive reply."""
    goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
    ws.send_json(goodbye)
    reply: list[Any] = ws.receive_json()
    return reply


# ===========================================================================
# Test: Multiple progressive CALLs accumulated
# ===========================================================================


class TestProgressiveCallInput:
    """Client sends multiple progressive CALLs to stream input to server."""

    def test_multiple_progressive_calls_accumulated(self) -> None:
        """Multiple progressive CALL chunks are accumulated and handler receives all."""
        hub = WampHub(realm="realm1")
        received_chunks: list[tuple[list[Any], dict[str, Any]]] = []

        @hub.register("com.example.stream")
        async def stream(
            _input_chunks: ProgressiveCallInput | None = None,
        ) -> str:
            if _input_chunks is not None:
                async for args, kwargs in _input_chunks:
                    received_chunks.append((args, kwargs))
            return "done"

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                # First progressive CALL (starts the handler)
                call1: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {"progress": True},
                    "com.example.stream",
                    ["chunk1"],
                ]
                ws.send_json(call1)

                # Second progressive CALL (same request_id)
                call2: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {"progress": True},
                    "com.example.stream",
                    ["chunk2"],
                ]
                ws.send_json(call2)

                # Third progressive CALL (same request_id)
                call3: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {"progress": True},
                    "com.example.stream",
                    ["chunk3"],
                ]
                ws.send_json(call3)

                # Final CALL (no progress flag — signals end of input)
                final_call: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {},
                    "com.example.stream",
                    ["chunk4"],
                ]
                ws.send_json(final_call)

                # Receive the final RESULT
                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 1  # same request_id
                assert result[3] == ["done"]

                _send_goodbye(ws)

        # Handler received all 4 chunks (3 progressive + 1 final)
        assert len(received_chunks) == 4
        assert received_chunks[0] == (["chunk1"], {})
        assert received_chunks[1] == (["chunk2"], {})
        assert received_chunks[2] == (["chunk3"], {})
        assert received_chunks[3] == (["chunk4"], {})

    def test_two_progressive_calls_then_final(self) -> None:
        """Handler receives exactly 2 progressive + 1 final chunk."""
        hub = WampHub(realm="realm1")
        chunks: list[list[Any]] = []

        @hub.register("com.example.two_chunks")
        async def two_chunks(
            _input_chunks: ProgressiveCallInput | None = None,
        ) -> int:
            if _input_chunks is not None:
                async for args, _kwargs in _input_chunks:
                    chunks.append(args)
            return len(chunks)

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                # Progressive chunk 1
                ws.send_json(
                    [
                        WampMessageType.CALL,
                        10,
                        {"progress": True},
                        "com.example.two_chunks",
                        [1],
                    ]
                )

                # Progressive chunk 2
                ws.send_json(
                    [
                        WampMessageType.CALL,
                        10,
                        {"progress": True},
                        "com.example.two_chunks",
                        [2],
                    ]
                )

                # Final chunk
                ws.send_json(
                    [
                        WampMessageType.CALL,
                        10,
                        {},
                        "com.example.two_chunks",
                        [3],
                    ]
                )

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 10
                assert result[3] == [3]  # 3 chunks

                _send_goodbye(ws)

        assert chunks == [[1], [2], [3]]

    def test_handler_receives_kwargs_in_chunks(self) -> None:
        """Progressive CALL chunks can include kwargs."""
        hub = WampHub(realm="realm1")
        received_kwargs: list[dict[str, Any]] = []

        @hub.register("com.example.kwargs_stream")
        async def kwargs_stream(
            _input_chunks: ProgressiveCallInput | None = None,
        ) -> str:
            if _input_chunks is not None:
                async for _args, kwargs in _input_chunks:
                    received_kwargs.append(kwargs)
            return "ok"

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                # Progressive chunk with kwargs
                ws.send_json(
                    [
                        WampMessageType.CALL,
                        5,
                        {"progress": True},
                        "com.example.kwargs_stream",
                        [],
                        {"key": "value1"},
                    ]
                )

                # Final chunk with kwargs
                ws.send_json(
                    [
                        WampMessageType.CALL,
                        5,
                        {},
                        "com.example.kwargs_stream",
                        [],
                        {"key": "value2"},
                    ]
                )

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[3] == ["ok"]

                _send_goodbye(ws)

        assert received_kwargs == [{"key": "value1"}, {"key": "value2"}]


# ===========================================================================
# Test: Non-existent procedure on progressive CALL
# ===========================================================================


class TestProgressiveCallErrors:
    """Error handling for progressive call invocations."""

    def test_nonexistent_procedure_first_progressive_call(self) -> None:
        """First progressive CALL for non-existent procedure returns error."""
        hub = WampHub(realm="realm1")

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                # Progressive CALL for non-existent procedure
                ws.send_json(
                    [
                        WampMessageType.CALL,
                        1,
                        {"progress": True},
                        "com.example.nonexistent",
                        ["data"],
                    ]
                )

                # Should get ERROR
                error: list[Any] = ws.receive_json()
                assert error[0] == WampMessageType.ERROR
                assert error[1] == WampMessageType.CALL
                assert error[2] == 1
                assert error[4] == "wamp.error.no_such_procedure"

                _send_goodbye(ws)


# ===========================================================================
# Test: Handler produces result after all input received
# ===========================================================================


class TestProgressiveCallResult:
    """Handler processes all input and produces final result."""

    def test_handler_aggregates_chunks(self) -> None:
        """Handler sums values from all progressive chunks."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.sum_stream")
        async def sum_stream(
            _input_chunks: ProgressiveCallInput | None = None,
        ) -> int:
            total = 0
            if _input_chunks is not None:
                async for args, _kwargs in _input_chunks:
                    if args:
                        total += args[0]
            return total

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                # Send progressive chunks: 10, 20, 30
                for val in [10, 20, 30]:
                    ws.send_json(
                        [
                            WampMessageType.CALL,
                            100,
                            {"progress": True},
                            "com.example.sum_stream",
                            [val],
                        ]
                    )

                # Final call with 40
                ws.send_json(
                    [
                        WampMessageType.CALL,
                        100,
                        {},
                        "com.example.sum_stream",
                        [40],
                    ]
                )

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 100
                assert result[3] == [100]  # 10+20+30+40

                _send_goodbye(ws)

    def test_handler_collects_strings(self) -> None:
        """Handler concatenates string chunks."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.concat_stream")
        async def concat_stream(
            _input_chunks: ProgressiveCallInput | None = None,
        ) -> str:
            parts: list[str] = []
            if _input_chunks is not None:
                async for args, _kwargs in _input_chunks:
                    if args:
                        parts.append(str(args[0]))
            return ",".join(parts)

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                ws.send_json(
                    [
                        WampMessageType.CALL,
                        7,
                        {"progress": True},
                        "com.example.concat_stream",
                        ["hello"],
                    ]
                )

                ws.send_json(
                    [
                        WampMessageType.CALL,
                        7,
                        {"progress": True},
                        "com.example.concat_stream",
                        ["world"],
                    ]
                )

                ws.send_json(
                    [
                        WampMessageType.CALL,
                        7,
                        {},
                        "com.example.concat_stream",
                        ["!"],
                    ]
                )

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[3] == ["hello,world,!"]

                _send_goodbye(ws)


# ===========================================================================
# Test: Regular calls still work (no regression)
# ===========================================================================


class TestRegularCallStillWorks:
    """Regular (non-progressive) calls should still work after the change."""

    def test_regular_call_unaffected(self) -> None:
        """Standard CALL without progress flag works as before."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.add")
        async def add(a: int, b: int) -> int:
            return a + b

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                ws.send_json(
                    [
                        WampMessageType.CALL,
                        1,
                        {},
                        "com.example.add",
                        [3, 4],
                    ]
                )

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 1
                assert result[3] == [7]

                _send_goodbye(ws)

    def test_regular_call_with_receive_progress_still_works(self) -> None:
        """CALL with receive_progress (output streaming) still works."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.multiply")
        async def multiply(a: int, b: int) -> int:
            return a * b

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                ws.send_json(
                    [
                        WampMessageType.CALL,
                        2,
                        {"receive_progress": True},
                        "com.example.multiply",
                        [5, 6],
                    ]
                )

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[3] == [30]

                _send_goodbye(ws)


# ===========================================================================
# Test: ProgressiveCallInput async iterator
# ===========================================================================


class TestProgressiveCallInputIterator:
    """Unit tests for the ProgressiveCallInput async iterator class."""

    async def test_single_chunk_and_end(self) -> None:
        """Iterator yields one chunk then stops."""
        import asyncio

        from fastapi_headless_wamp.session import PROGRESSIVE_INPUT_END

        queue: asyncio.Queue[Any] = asyncio.Queue()
        pci = ProgressiveCallInput(queue)

        queue.put_nowait((["data"], {"k": "v"}))
        queue.put_nowait(PROGRESSIVE_INPUT_END)

        result: list[tuple[list[Any], dict[str, Any]]] = []
        async for args, kwargs in pci:
            result.append((args, kwargs))
        assert result == [(["data"], {"k": "v"})]

    async def test_multiple_chunks(self) -> None:
        """Iterator yields multiple chunks in order."""
        import asyncio

        from fastapi_headless_wamp.session import PROGRESSIVE_INPUT_END

        queue: asyncio.Queue[Any] = asyncio.Queue()
        pci = ProgressiveCallInput(queue)

        queue.put_nowait(([1], {}))
        queue.put_nowait(([2], {}))
        queue.put_nowait(([3], {}))
        queue.put_nowait(PROGRESSIVE_INPUT_END)

        result: list[tuple[list[Any], dict[str, Any]]] = []
        async for args, kwargs in pci:
            result.append((args, kwargs))
        assert len(result) == 3
        assert result[0] == ([1], {})
        assert result[1] == ([2], {})
        assert result[2] == ([3], {})

    async def test_empty_stream(self) -> None:
        """Iterator with just end sentinel yields nothing."""
        import asyncio

        from fastapi_headless_wamp.session import PROGRESSIVE_INPUT_END

        queue: asyncio.Queue[Any] = asyncio.Queue()
        pci = ProgressiveCallInput(queue)

        queue.put_nowait(PROGRESSIVE_INPUT_END)

        result: list[tuple[list[Any], dict[str, Any]]] = []
        async for args, kwargs in pci:
            result.append((args, kwargs))
        assert result == []


# ===========================================================================
# Test: Progressive input with empty args
# ===========================================================================


class TestProgressiveInputEdgeCases:
    """Edge cases for progressive call invocations."""

    def test_progressive_calls_with_empty_args(self) -> None:
        """Progressive CALL chunks with no args."""
        hub = WampHub(realm="realm1")
        chunk_count = 0

        @hub.register("com.example.count_chunks")
        async def count_chunks(
            _input_chunks: ProgressiveCallInput | None = None,
        ) -> int:
            nonlocal chunk_count
            if _input_chunks is not None:
                async for _args, _kwargs in _input_chunks:
                    chunk_count += 1
            return chunk_count

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                # Progressive chunk with no args
                ws.send_json(
                    [
                        WampMessageType.CALL,
                        20,
                        {"progress": True},
                        "com.example.count_chunks",
                    ]
                )

                # Another progressive chunk with no args
                ws.send_json(
                    [
                        WampMessageType.CALL,
                        20,
                        {"progress": True},
                        "com.example.count_chunks",
                    ]
                )

                # Final chunk
                ws.send_json(
                    [
                        WampMessageType.CALL,
                        20,
                        {},
                        "com.example.count_chunks",
                    ]
                )

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[3] == [3]  # 3 chunks

                _send_goodbye(ws)

    def test_final_result_sent_after_all_chunks(self) -> None:
        """The RESULT is only sent after the handler processes all chunks."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.delayed_result")
        async def delayed_result(
            _input_chunks: ProgressiveCallInput | None = None,
        ) -> str:
            items: list[str] = []
            if _input_chunks is not None:
                async for args, _kwargs in _input_chunks:
                    if args:
                        items.append(str(args[0]))
            return f"received:{len(items)}"

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                # Send 5 progressive chunks then final
                for i in range(5):
                    ws.send_json(
                        [
                            WampMessageType.CALL,
                            50,
                            {"progress": True},
                            "com.example.delayed_result",
                            [f"item{i}"],
                        ]
                    )

                ws.send_json(
                    [
                        WampMessageType.CALL,
                        50,
                        {},
                        "com.example.delayed_result",
                        ["final_item"],
                    ]
                )

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[3] == ["received:6"]

                _send_goodbye(ws)
