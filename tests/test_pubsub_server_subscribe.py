"""Tests for PubSub: client publishes, server receives (US-019).

Covers:
- @wamp.subscribe(topic) decorator registers server-side event handlers on hub
- Client PUBLISH on a topic with a registered server handler invokes the handler
- Handler receives event args/kwargs and session
- PUBLISHED acknowledgment when options.acknowledge is true
- Publisher exclusion (exclude_me) and blackwhite listing parsed correctly
- Class-based @subscribe(topic) on WampService methods
- Multiple handlers for the same topic
- Publish to topic with no handler (no error)
- Sync handler support
- Handler exception does not crash the session
- FastAPI integration tests
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import FastAPI
from fastapi import WebSocket as FastAPIWebSocket
from starlette.testclient import TestClient

from fastapi_headless_wamp.hub import WampHub
from fastapi_headless_wamp.protocol import WampMessageType
from fastapi_headless_wamp.service import WampService, subscribe
from fastapi_headless_wamp.session import WampSession

# ---------------------------------------------------------------------------
# Mock WebSocket (reusable helper)
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


def find_msg_by_type(ws: MockWebSocket, msg_type: int) -> list[Any] | None:
    """Find the first sent message with the given type code."""
    for text in ws.sent_texts:
        msg: list[Any] = json.loads(text)
        if msg[0] == msg_type:
            return msg
    return None


def find_all_msgs_by_type(ws: MockWebSocket, msg_type: int) -> list[list[Any]]:
    """Find all sent messages with the given type code."""
    results: list[list[Any]] = []
    for text in ws.sent_texts:
        msg: list[Any] = json.loads(text)
        if msg[0] == msg_type:
            results.append(msg)
    return results


# ---------------------------------------------------------------------------
# @wamp.subscribe(topic) decorator tests
# ---------------------------------------------------------------------------


class TestSubscribeDecorator:
    """Tests for @wamp.subscribe(topic) decorator on WampHub."""

    def test_subscribe_decorator_registers_handler(self) -> None:
        """@wamp.subscribe(topic) stores the handler in _server_subscriptions."""
        hub = WampHub(realm="realm1")

        @hub.subscribe("com.example.event")
        async def on_event(session: Any) -> None:
            pass

        subs = hub._server_subscriptions
        assert "com.example.event" in subs
        assert len(subs["com.example.event"]) == 1

    def test_subscribe_decorator_returns_function_unchanged(self) -> None:
        """@wamp.subscribe(topic) returns the original function."""
        hub = WampHub(realm="realm1")

        async def on_event(session: Any) -> None:
            pass

        result = hub.subscribe("com.example.event")(on_event)
        assert result is on_event

    def test_multiple_handlers_same_topic(self) -> None:
        """Multiple handlers can be registered for the same topic."""
        hub = WampHub(realm="realm1")

        @hub.subscribe("com.example.event")
        async def handler1(session: Any) -> None:
            pass

        @hub.subscribe("com.example.event")
        async def handler2(session: Any) -> None:
            pass

        subs = hub._server_subscriptions
        assert len(subs["com.example.event"]) == 2


# ---------------------------------------------------------------------------
# Client PUBLISH -> server handler invoked (mock-based tests)
# ---------------------------------------------------------------------------


class TestHandlePublish:
    """Tests for _handle_publish dispatching to server handlers."""

    async def test_client_publish_invokes_handler(self) -> None:
        """Client PUBLISH on a topic with a registered handler invokes it."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        received: list[tuple[Any, ...]] = []

        @hub.subscribe("com.example.event")
        async def on_event(session: Any, x: int, y: int) -> None:
            received.append((x, y))

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        publish: list[Any] = [
            WampMessageType.PUBLISH,
            1,
            {},
            "com.example.event",
            [10, 20],
        ]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(publish))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert received == [(10, 20)]

    async def test_client_publish_with_kwargs(self) -> None:
        """Client PUBLISH with kwargs passes them to the handler."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        received_kwargs: list[dict[str, Any]] = []

        @hub.subscribe("com.example.event")
        async def on_event(session: Any, **kwargs: Any) -> None:
            received_kwargs.append(kwargs)

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        publish: list[Any] = [
            WampMessageType.PUBLISH,
            1,
            {},
            "com.example.event",
            [],
            {"name": "Alice", "age": 30},
        ]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(publish))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert len(received_kwargs) == 1
        assert received_kwargs[0]["name"] == "Alice"
        assert received_kwargs[0]["age"] == 30

    async def test_client_publish_handler_receives_session(self) -> None:
        """Handler receives the WampSession as first positional arg."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        received_sessions: list[WampSession] = []

        @hub.subscribe("com.example.event")
        async def on_event(session: WampSession) -> None:
            received_sessions.append(session)

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        publish: list[Any] = [
            WampMessageType.PUBLISH,
            1,
            {},
            "com.example.event",
        ]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(publish))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert len(received_sessions) == 1
        assert isinstance(received_sessions[0], WampSession)

    async def test_client_publish_no_handler_no_error(self) -> None:
        """PUBLISH on a topic with no server handler is silently ignored."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        publish: list[Any] = [
            WampMessageType.PUBLISH,
            1,
            {},
            "com.no.handler",
            [1],
        ]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(publish))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Only WELCOME + GOODBYE reply; no error sent
        assert len(ws.sent_texts) == 2

    async def test_client_publish_acknowledge(self) -> None:
        """When options.acknowledge is true, server sends PUBLISHED back."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        @hub.subscribe("com.example.event")
        async def on_event(session: Any) -> None:
            pass

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        publish: list[Any] = [
            WampMessageType.PUBLISH,
            42,
            {"acknowledge": True},
            "com.example.event",
            [1],
        ]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(publish))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        published = find_msg_by_type(ws, WampMessageType.PUBLISHED)
        assert published is not None
        assert published[1] == 42  # request_id echoed
        assert isinstance(published[2], int)
        assert published[2] > 0  # publication_id

    async def test_client_publish_acknowledge_no_handler(self) -> None:
        """Acknowledge works even when there is no server handler for the topic."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        publish: list[Any] = [
            WampMessageType.PUBLISH,
            7,
            {"acknowledge": True},
            "com.no.handler",
        ]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(publish))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        published = find_msg_by_type(ws, WampMessageType.PUBLISHED)
        assert published is not None
        assert published[1] == 7

    async def test_client_publish_no_acknowledge(self) -> None:
        """Without acknowledge, no PUBLISHED is sent back."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        @hub.subscribe("com.example.event")
        async def on_event(session: Any) -> None:
            pass

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        publish: list[Any] = [
            WampMessageType.PUBLISH,
            1,
            {},
            "com.example.event",
        ]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(publish))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Only WELCOME + GOODBYE reply; no PUBLISHED
        published = find_msg_by_type(ws, WampMessageType.PUBLISHED)
        assert published is None

    async def test_client_publish_sync_handler(self) -> None:
        """Sync handlers are called via asyncio.to_thread."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        received: list[int] = []

        @hub.subscribe("com.example.sync_event")
        def on_event(session: Any, x: int) -> None:
            received.append(x)

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        publish: list[Any] = [
            WampMessageType.PUBLISH,
            1,
            {},
            "com.example.sync_event",
            [99],
        ]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(publish))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert received == [99]

    async def test_client_publish_handler_exception_no_crash(self) -> None:
        """Handler exception is logged but does not crash the session."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        @hub.subscribe("com.example.event")
        async def on_event(session: Any) -> None:
            raise ValueError("handler error")

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        publish: list[Any] = [
            WampMessageType.PUBLISH,
            1,
            {},
            "com.example.event",
        ]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(publish))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Session should still complete normally: WELCOME + GOODBYE
        assert len(ws.sent_texts) == 2

    async def test_multiple_handlers_all_invoked(self) -> None:
        """Multiple handlers for the same topic are all invoked."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        results: list[str] = []

        @hub.subscribe("com.example.event")
        async def handler1(session: Any) -> None:
            results.append("handler1")

        @hub.subscribe("com.example.event")
        async def handler2(session: Any) -> None:
            results.append("handler2")

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        publish: list[Any] = [
            WampMessageType.PUBLISH,
            1,
            {},
            "com.example.event",
        ]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(publish))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert results == ["handler1", "handler2"]

    async def test_one_handler_failure_doesnt_stop_others(self) -> None:
        """If one handler fails, the rest still execute."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        results: list[str] = []

        @hub.subscribe("com.example.event")
        async def handler1(session: Any) -> None:
            raise RuntimeError("boom")

        @hub.subscribe("com.example.event")
        async def handler2(session: Any) -> None:
            results.append("handler2")

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        publish: list[Any] = [
            WampMessageType.PUBLISH,
            1,
            {},
            "com.example.event",
        ]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(publish))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert results == ["handler2"]

    async def test_publish_with_exclude_me_option(self) -> None:
        """Publisher exclusion (exclude_me) option is parsed without error."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        received: list[bool] = []

        @hub.subscribe("com.example.event")
        async def on_event(session: Any) -> None:
            received.append(True)

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        publish: list[Any] = [
            WampMessageType.PUBLISH,
            1,
            {"exclude_me": False},
            "com.example.event",
        ]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(publish))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert received == [True]

    async def test_publish_with_eligible_exclude_options(self) -> None:
        """Subscriber blackwhite listing options (eligible/exclude) are parsed without error."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        received: list[bool] = []

        @hub.subscribe("com.example.event")
        async def on_event(session: Any) -> None:
            received.append(True)

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        publish: list[Any] = [
            WampMessageType.PUBLISH,
            1,
            {"eligible": [1, 2, 3], "exclude": [4, 5]},
            "com.example.event",
        ]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(publish))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert received == [True]

    async def test_publication_ids_are_unique(self) -> None:
        """Multiple acknowledged PUBLISHes get unique publication IDs."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        pub1: list[Any] = [
            WampMessageType.PUBLISH,
            1,
            {"acknowledge": True},
            "com.example.event",
        ]
        pub2: list[Any] = [
            WampMessageType.PUBLISH,
            2,
            {"acknowledge": True},
            "com.example.event",
        ]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(pub1))
        ws.enqueue_text(json.dumps(pub2))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        published_msgs = find_all_msgs_by_type(ws, WampMessageType.PUBLISHED)
        assert len(published_msgs) == 2
        assert published_msgs[0][2] != published_msgs[1][2]


# ---------------------------------------------------------------------------
# Class-based WampService with @subscribe decorator
# ---------------------------------------------------------------------------


class TestServiceSubscribe:
    """Tests for @subscribe(topic) on WampService methods."""

    async def test_class_subscribe_invoked(self) -> None:
        """@subscribe on a WampService method is invoked when client publishes."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        results: list[Any] = []

        class EventService(WampService):
            prefix = "com.events"

            @subscribe("on_data")
            async def handle_data(self, session: Any, value: int) -> None:
                results.append(value)

        hub.register_service(EventService())

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        publish: list[Any] = [
            WampMessageType.PUBLISH,
            1,
            {},
            "com.events.on_data",
            [42],
        ]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(publish))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert results == [42]

    async def test_class_subscribe_no_prefix(self) -> None:
        """@subscribe on a WampService with no prefix uses topic as-is."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        results: list[Any] = []

        class NoPrefix(WampService):
            @subscribe("my.event")
            async def handle_event(self, session: Any, val: str) -> None:
                results.append(val)

        hub.register_service(NoPrefix())

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        publish: list[Any] = [
            WampMessageType.PUBLISH,
            1,
            {},
            "my.event",
            ["hello"],
        ]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(publish))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert results == ["hello"]

    async def test_class_subscribe_with_acknowledge(self) -> None:
        """Class-based subscribe handler works with acknowledge mode."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        results: list[int] = []

        class Svc(WampService):
            prefix = "svc"

            @subscribe("event")
            async def handle(self, session: Any, x: int) -> None:
                results.append(x)

        hub.register_service(Svc())

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        publish: list[Any] = [
            WampMessageType.PUBLISH,
            5,
            {"acknowledge": True},
            "svc.event",
            [100],
        ]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(publish))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert results == [100]
        published = find_msg_by_type(ws, WampMessageType.PUBLISHED)
        assert published is not None
        assert published[1] == 5

    async def test_service_hub_accessible_from_subscribe_handler(self) -> None:
        """The service's hub attribute is accessible from @subscribe handlers."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        hub_refs: list[WampHub] = []

        class Svc(WampService):
            prefix = "svc"

            @subscribe("check_hub")
            async def handle(self, session: Any) -> None:
                if self.hub is not None:
                    hub_refs.append(self.hub)

        hub.register_service(Svc())

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        publish: list[Any] = [
            WampMessageType.PUBLISH,
            1,
            {},
            "svc.check_hub",
        ]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(publish))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert len(hub_refs) == 1
        assert hub_refs[0] is hub


# ---------------------------------------------------------------------------
# FastAPI integration tests
# ---------------------------------------------------------------------------


class TestFastAPIIntegration:
    """Integration tests using FastAPI test client."""

    def _make_app(self, hub: WampHub) -> FastAPI:
        """Create a FastAPI app with a /ws endpoint."""
        app = FastAPI()

        @app.websocket("/ws")
        async def wamp_endpoint(websocket: FastAPIWebSocket) -> None:
            await hub.handle_websocket(websocket)

        return app

    def _do_handshake(self, ws: Any) -> int:
        """Send HELLO, receive WELCOME, return session_id."""
        hello: list[Any] = [
            WampMessageType.HELLO,
            "realm1",
            {"roles": {"publisher": {}}},
        ]
        ws.send_json(hello)
        welcome: list[Any] = ws.receive_json()
        assert welcome[0] == WampMessageType.WELCOME
        session_id: int = welcome[1]
        return session_id

    def _send_goodbye(self, ws: Any) -> list[Any]:
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.send_json(goodbye)
        reply: list[Any] = ws.receive_json()
        return reply

    def test_client_publish_invokes_handler_fastapi(self) -> None:
        """FastAPI: client PUBLISH invokes server subscription handler."""
        hub = WampHub(realm="realm1")

        received: list[tuple[Any, ...]] = []

        @hub.subscribe("com.example.event")
        async def on_event(session: Any, msg: str) -> None:
            received.append((msg,))

        app = self._make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                self._do_handshake(ws)

                publish: list[Any] = [
                    WampMessageType.PUBLISH,
                    1,
                    {},
                    "com.example.event",
                    ["hello world"],
                ]
                ws.send_json(publish)

                self._send_goodbye(ws)

        assert received == [("hello world",)]

    def test_client_publish_acknowledge_fastapi(self) -> None:
        """FastAPI: client PUBLISH with acknowledge gets PUBLISHED back."""
        hub = WampHub(realm="realm1")

        @hub.subscribe("com.example.event")
        async def on_event(session: Any) -> None:
            pass

        app = self._make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                self._do_handshake(ws)

                publish: list[Any] = [
                    WampMessageType.PUBLISH,
                    10,
                    {"acknowledge": True},
                    "com.example.event",
                ]
                ws.send_json(publish)
                published: list[Any] = ws.receive_json()
                assert published[0] == WampMessageType.PUBLISHED
                assert published[1] == 10
                assert isinstance(published[2], int)

                self._send_goodbye(ws)

    def test_client_publish_no_handler_fastapi(self) -> None:
        """FastAPI: PUBLISH on a topic with no handler is silently ignored."""
        hub = WampHub(realm="realm1")
        app = self._make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                self._do_handshake(ws)

                publish: list[Any] = [
                    WampMessageType.PUBLISH,
                    1,
                    {},
                    "com.no.handler",
                ]
                ws.send_json(publish)

                # Should be able to send GOODBYE without any error
                goodbye_reply = self._send_goodbye(ws)
                assert goodbye_reply[0] == WampMessageType.GOODBYE

    def test_class_based_subscribe_fastapi(self) -> None:
        """FastAPI: class-based @subscribe handler is invoked."""
        hub = WampHub(realm="realm1")

        results: list[Any] = []

        class Svc(WampService):
            prefix = "svc"

            @subscribe("on_data")
            async def handle_data(self, session: Any, x: int) -> None:
                results.append(x)

        hub.register_service(Svc())
        app = self._make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                self._do_handshake(ws)

                publish: list[Any] = [
                    WampMessageType.PUBLISH,
                    1,
                    {},
                    "svc.on_data",
                    [55],
                ]
                ws.send_json(publish)

                self._send_goodbye(ws)

        assert results == [55]

    def test_acknowledge_with_no_handler_fastapi(self) -> None:
        """FastAPI: acknowledge works even when there is no handler."""
        hub = WampHub(realm="realm1")
        app = self._make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                self._do_handshake(ws)

                publish: list[Any] = [
                    WampMessageType.PUBLISH,
                    3,
                    {"acknowledge": True},
                    "com.no.handler",
                ]
                ws.send_json(publish)
                published: list[Any] = ws.receive_json()
                assert published[0] == WampMessageType.PUBLISHED
                assert published[1] == 3

                self._send_goodbye(ws)

    def test_multiple_publishes_fastapi(self) -> None:
        """FastAPI: multiple PUBLISHes invoke handler for each."""
        hub = WampHub(realm="realm1")

        results: list[int] = []

        @hub.subscribe("com.example.counter")
        async def on_counter(session: Any, n: int) -> None:
            results.append(n)

        app = self._make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                self._do_handshake(ws)

                for i in range(5):
                    publish: list[Any] = [
                        WampMessageType.PUBLISH,
                        i + 1,
                        {},
                        "com.example.counter",
                        [i],
                    ]
                    ws.send_json(publish)

                self._send_goodbye(ws)

        assert results == [0, 1, 2, 3, 4]
