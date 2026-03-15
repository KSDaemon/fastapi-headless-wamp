"""Tests for progressive call results (US-012).

Covers:
- When client sends CALL with receive_progress=true, handler receives a _progress
  async callback parameter
- Calling await _progress(data) sends RESULT with details.progress=true to client
- Handler's final return value sends RESULT without progress flag
- If client does not request progressive results, _progress parameter is None
- Request ID tracked throughout the progressive exchange
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import FastAPI, WebSocket as FastAPIWebSocket
from starlette.testclient import TestClient

from fastapi_headless_wamp.hub import WampHub
from fastapi_headless_wamp.protocol import (
    WampMessageType,
)


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
# Test: Progressive results sent and received
# ===========================================================================


class TestProgressiveResults:
    """Progressive results: handler sends intermediate results via _progress."""

    def test_progressive_results_sent(self) -> None:
        """Handler sends progressive results then final result."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.count")
        async def count(
            n: int,
            _progress: Callable[[Any], Coroutine[Any, Any, None]] | None = None,
        ) -> str:
            for i in range(n):
                if _progress is not None:
                    await _progress(i)
            return "done"

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                # Send CALL with receive_progress=true
                call: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {"receive_progress": True},
                    "com.example.count",
                    [3],
                ]
                ws.send_json(call)

                # Receive 3 progressive results
                for i in range(3):
                    msg: list[Any] = ws.receive_json()
                    assert msg[0] == WampMessageType.RESULT
                    assert msg[1] == 1  # same request_id
                    assert msg[2] == {"progress": True}
                    assert msg[3] == [i]

                # Receive final result
                final: list[Any] = ws.receive_json()
                assert final[0] == WampMessageType.RESULT
                assert final[1] == 1  # same request_id
                assert final[2] == {}  # no progress flag
                assert final[3] == ["done"]

                _send_goodbye(ws)

    def test_progressive_single_progress(self) -> None:
        """Handler sends exactly one progressive result then final."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.single_progress")
        async def single_progress(
            _progress: Callable[[Any], Coroutine[Any, Any, None]] | None = None,
        ) -> str:
            if _progress is not None:
                await _progress("intermediate")
            return "final"

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    5,
                    {"receive_progress": True},
                    "com.example.single_progress",
                ]
                ws.send_json(call)

                # Progressive result
                progress_msg: list[Any] = ws.receive_json()
                assert progress_msg[0] == WampMessageType.RESULT
                assert progress_msg[1] == 5
                assert progress_msg[2] == {"progress": True}
                assert progress_msg[3] == ["intermediate"]

                # Final result
                final: list[Any] = ws.receive_json()
                assert final[0] == WampMessageType.RESULT
                assert final[1] == 5
                assert final[2] == {}
                assert final[3] == ["final"]

                _send_goodbye(ws)

    def test_progressive_no_data(self) -> None:
        """Progressive result with None data sends RESULT without args."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.progress_none")
        async def progress_none(
            _progress: Callable[[Any], Coroutine[Any, Any, None]] | None = None,
        ) -> str:
            if _progress is not None:
                await _progress(None)
            return "result"

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    10,
                    {"receive_progress": True},
                    "com.example.progress_none",
                ]
                ws.send_json(call)

                # Progressive result with no data
                progress_msg: list[Any] = ws.receive_json()
                assert progress_msg[0] == WampMessageType.RESULT
                assert progress_msg[1] == 10
                assert progress_msg[2] == {"progress": True}
                # No args element when data is None
                assert len(progress_msg) == 3

                # Final result
                final: list[Any] = ws.receive_json()
                assert final[0] == WampMessageType.RESULT
                assert final[3] == ["result"]

                _send_goodbye(ws)

    def test_request_id_tracked_throughout(self) -> None:
        """Request ID is the same for all progressive and final results."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.tracked")
        async def tracked(
            _progress: Callable[[Any], Coroutine[Any, Any, None]] | None = None,
        ) -> str:
            if _progress is not None:
                await _progress("a")
                await _progress("b")
                await _progress("c")
            return "final"

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                req_id = 42
                call: list[Any] = [
                    WampMessageType.CALL,
                    req_id,
                    {"receive_progress": True},
                    "com.example.tracked",
                ]
                ws.send_json(call)

                # All progressive results have the same request_id
                for expected in ["a", "b", "c"]:
                    msg: list[Any] = ws.receive_json()
                    assert msg[1] == req_id
                    assert msg[2] == {"progress": True}
                    assert msg[3] == [expected]

                # Final result
                final: list[Any] = ws.receive_json()
                assert final[1] == req_id
                assert final[2] == {}

                _send_goodbye(ws)


# ===========================================================================
# Test: Non-progressive call has _progress=None
# ===========================================================================


class TestNonProgressiveCall:
    """If client does not request progressive results, _progress is None."""

    def test_progress_is_none_without_receive_progress(self) -> None:
        """Handler gets _progress=None when receive_progress not set."""
        hub = WampHub(realm="realm1")
        progress_values: list[Any] = []

        @hub.register("com.example.check_progress")
        async def check_progress(
            _progress: Callable[[Any], Coroutine[Any, Any, None]] | None = None,
        ) -> str:
            progress_values.append(_progress)
            return "ok"

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                # CALL without receive_progress
                call: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {},
                    "com.example.check_progress",
                ]
                ws.send_json(call)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[3] == ["ok"]

                _send_goodbye(ws)

        # _progress was None
        assert progress_values[0] is None

    def test_progress_is_none_with_receive_progress_false(self) -> None:
        """Handler gets _progress=None when receive_progress is explicitly false."""
        hub = WampHub(realm="realm1")
        progress_values: list[Any] = []

        @hub.register("com.example.check_progress2")
        async def check_progress2(
            _progress: Callable[[Any], Coroutine[Any, Any, None]] | None = None,
        ) -> str:
            progress_values.append(_progress)
            return "ok"

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    2,
                    {"receive_progress": False},
                    "com.example.check_progress2",
                ]
                ws.send_json(call)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT

                _send_goodbye(ws)

        assert progress_values[0] is None

    def test_handler_without_progress_param_works_normally(self) -> None:
        """Handler without _progress parameter still works with receive_progress=true."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.simple")
        async def simple(x: int) -> int:
            return x * 2

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                # Even with receive_progress, if handler doesn't have _progress param
                # it should still work (the _progress kwarg is passed but handler
                # may accept **kwargs or the hub should handle it gracefully)
                call: list[Any] = [
                    WampMessageType.CALL,
                    3,
                    {"receive_progress": True},
                    "com.example.simple",
                    [5],
                ]
                ws.send_json(call)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 3
                assert result[3] == [10]

                _send_goodbye(ws)

    def test_handler_without_progress_param_no_receive_progress(self) -> None:
        """Handler without _progress parameter works normally without receive_progress."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.basic")
        async def basic(x: int) -> int:
            return x + 1

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    4,
                    {},
                    "com.example.basic",
                    [10],
                ]
                ws.send_json(call)

                result: list[Any] = ws.receive_json()
                assert result[0] == WampMessageType.RESULT
                assert result[1] == 4
                assert result[3] == [11]

                _send_goodbye(ws)


# ===========================================================================
# Test: Progressive with complex data types
# ===========================================================================


class TestProgressiveComplexData:
    """Progressive results with various data types."""

    def test_progressive_with_dict_data(self) -> None:
        """Progressive results can carry dict data."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.dict_progress")
        async def dict_progress(
            _progress: Callable[[Any], Coroutine[Any, Any, None]] | None = None,
        ) -> dict[str, Any]:
            if _progress is not None:
                await _progress({"step": 1, "status": "loading"})
                await _progress({"step": 2, "status": "processing"})
            return {"step": 3, "status": "complete"}

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {"receive_progress": True},
                    "com.example.dict_progress",
                ]
                ws.send_json(call)

                p1: list[Any] = ws.receive_json()
                assert p1[2] == {"progress": True}
                assert p1[3] == [{"step": 1, "status": "loading"}]

                p2: list[Any] = ws.receive_json()
                assert p2[2] == {"progress": True}
                assert p2[3] == [{"step": 2, "status": "processing"}]

                final: list[Any] = ws.receive_json()
                assert final[2] == {}
                assert final[3] == [{"step": 3, "status": "complete"}]

                _send_goodbye(ws)

    def test_progressive_with_list_data(self) -> None:
        """Progressive results can carry list data."""
        hub = WampHub(realm="realm1")

        @hub.register("com.example.list_progress")
        async def list_progress(
            _progress: Callable[[Any], Coroutine[Any, Any, None]] | None = None,
        ) -> list[int]:
            if _progress is not None:
                await _progress([1, 2, 3])
            return [4, 5, 6]

        app = _make_app(hub)
        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                _do_handshake(ws)

                call: list[Any] = [
                    WampMessageType.CALL,
                    1,
                    {"receive_progress": True},
                    "com.example.list_progress",
                ]
                ws.send_json(call)

                p1: list[Any] = ws.receive_json()
                assert p1[2] == {"progress": True}
                assert p1[3] == [[1, 2, 3]]

                final: list[Any] = ws.receive_json()
                assert final[2] == {}
                assert final[3] == [[4, 5, 6]]

                _send_goodbye(ws)
