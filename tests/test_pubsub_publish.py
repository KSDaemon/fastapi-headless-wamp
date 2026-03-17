"""Tests for PubSub: server publishes events to client (US-018).

Covers:
- session.publish(topic, args, kwargs) sends EVENT to subscribed client
- EVENT format: [EVENT, subscription_id, publication_id, details, args?, kwargs?]
- Publication IDs are unique and incrementing per session
- Publish to unsubscribed client is a no-op (no error)
- publisher_identification: details.publisher included when specified
- hub.publish_to_all(topic, args, kwargs) publishes to all subscribed sessions
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
# Helper: create a session with subscriptions
# ---------------------------------------------------------------------------


async def make_subscribed_session(
    hub: WampHub, topics: list[str]
) -> tuple[MockWebSocket, WampSession]:
    """Create a hub session that is subscribed to the given topics.

    Returns ``(mock_websocket, session)`` where the session has completed
    the WAMP handshake and has active subscriptions.
    """
    ws = MockWebSocket(subprotocols=["wamp.2.json"])

    # Queue HELLO
    hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
    ws.enqueue_text(json.dumps(hello))

    # Queue SUBSCRIBE for each topic
    for i, topic in enumerate(topics, start=1):
        sub: list[Any] = [WampMessageType.SUBSCRIBE, i, {}, topic]
        ws.enqueue_text(json.dumps(sub))

    session_ref: list[WampSession] = []

    @hub.on_session_open
    async def capture_session(session: WampSession) -> None:
        session_ref.append(session)

    # Hook the message loop to process subscribes then keep the session alive
    # by storing it for later use.
    original_loop = hub._message_loop  # type: ignore[attr-defined]
    ready_event = asyncio.Event()
    keep_alive_event = asyncio.Event()

    async def hooked_loop(session: WampSession) -> None:
        # Process all SUBSCRIBE messages
        for _ in topics:
            msg = await session.receive_message()
            await hub._handle_subscribe(session, msg)  # type: ignore[attr-defined]
        ready_event.set()
        # Block until caller signals done
        await keep_alive_event.wait()

    hub._message_loop = hooked_loop  # type: ignore[method-assign]

    # Start the session in a background task
    task = asyncio.create_task(hub.handle_websocket(ws))  # type: ignore[arg-type]

    # Wait for session to be ready
    await ready_event.wait()

    # Restore original loop for future sessions
    hub._message_loop = original_loop  # type: ignore[method-assign]

    # Remove the on_session_open callback we added (to avoid interference)
    hub._on_session_open_callbacks.pop()  # type: ignore[attr-defined]

    session = session_ref[0]
    # Attach cleanup helpers for later use
    session._test_keep_alive_event = keep_alive_event  # type: ignore[attr-defined]
    session._test_task = task  # type: ignore[attr-defined]

    return ws, session


async def cleanup_session(session: WampSession) -> None:
    """Signal the session to close and await the background task."""
    keep_alive = getattr(session, "_test_keep_alive_event", None)
    task = getattr(session, "_test_task", None)
    if keep_alive is not None:
        keep_alive.set()
    if task is not None:
        await task


# ---------------------------------------------------------------------------
# session.publish() tests
# ---------------------------------------------------------------------------


class TestSessionPublish:
    """session.publish(topic, args, kwargs) sends EVENT to subscribed client."""

    async def test_publish_to_subscribed_client(self) -> None:
        """Publishing to a subscribed topic sends EVENT."""
        hub = WampHub(realm="realm1")
        ws, session = await make_subscribed_session(hub, ["com.example.topic"])
        try:
            await session.publish("com.example.topic", args=[42, "hello"])

            event = find_msg_by_type(ws, WampMessageType.EVENT)
            assert event is not None
            assert event[0] == WampMessageType.EVENT
            # subscription_id should match
            sub_id = session.subscription_uris.get("com.example.topic")
            assert event[1] == sub_id
            # publication_id should be a positive integer
            assert isinstance(event[2], int)
            assert event[2] > 0
            # details
            assert isinstance(event[3], dict)
            # args
            assert event[4] == [42, "hello"]
        finally:
            await cleanup_session(session)

    async def test_publish_with_kwargs(self) -> None:
        """Publishing with kwargs includes both args and kwargs in EVENT."""
        hub = WampHub(realm="realm1")
        ws, session = await make_subscribed_session(hub, ["com.topic"])
        try:
            await session.publish("com.topic", args=[1], kwargs={"key": "value"})

            event = find_msg_by_type(ws, WampMessageType.EVENT)
            assert event is not None
            assert event[4] == [1]
            assert event[5] == {"key": "value"}
        finally:
            await cleanup_session(session)

    async def test_publish_kwargs_only(self) -> None:
        """Publishing with only kwargs sends empty args list and kwargs."""
        hub = WampHub(realm="realm1")
        ws, session = await make_subscribed_session(hub, ["com.topic"])
        try:
            await session.publish("com.topic", kwargs={"data": 123})

            event = find_msg_by_type(ws, WampMessageType.EVENT)
            assert event is not None
            assert event[4] == []  # empty args placeholder
            assert event[5] == {"data": 123}
        finally:
            await cleanup_session(session)

    async def test_publish_no_args(self) -> None:
        """Publishing without args or kwargs sends EVENT with just details."""
        hub = WampHub(realm="realm1")
        ws, session = await make_subscribed_session(hub, ["com.topic"])
        try:
            await session.publish("com.topic")

            event = find_msg_by_type(ws, WampMessageType.EVENT)
            assert event is not None
            assert len(event) == 4  # [EVENT, sub_id, pub_id, details]
        finally:
            await cleanup_session(session)

    async def test_publish_to_unsubscribed_is_noop(self) -> None:
        """Publishing to a topic the client hasn't subscribed to is a no-op."""
        hub = WampHub(realm="realm1")
        ws, session = await make_subscribed_session(hub, ["com.other.topic"])
        try:
            # Publish to a different topic
            await session.publish("com.nonexistent.topic", args=[1, 2, 3])

            # No EVENT should be sent (only WELCOME and SUBSCRIBED exist)
            event = find_msg_by_type(ws, WampMessageType.EVENT)
            assert event is None
        finally:
            await cleanup_session(session)

    async def test_publication_ids_incrementing(self) -> None:
        """Publication IDs are unique and incrementing per session."""
        hub = WampHub(realm="realm1")
        ws, session = await make_subscribed_session(hub, ["com.topic"])
        try:
            await session.publish("com.topic", args=["first"])
            await session.publish("com.topic", args=["second"])
            await session.publish("com.topic", args=["third"])

            events = find_all_msgs_by_type(ws, WampMessageType.EVENT)
            assert len(events) == 3

            pub_ids = [e[2] for e in events]
            # Should be incrementing
            assert pub_ids[0] < pub_ids[1] < pub_ids[2]
            # Should be unique
            assert len(set(pub_ids)) == 3
        finally:
            await cleanup_session(session)

    async def test_publisher_identification(self) -> None:
        """When publisher is set, details.publisher is included in EVENT."""
        hub = WampHub(realm="realm1")
        ws, session = await make_subscribed_session(hub, ["com.topic"])
        try:
            await session.publish("com.topic", args=["data"], publisher=12345)

            event = find_msg_by_type(ws, WampMessageType.EVENT)
            assert event is not None
            details = event[3]
            assert details.get("publisher") == 12345
        finally:
            await cleanup_session(session)

    async def test_publisher_not_set_no_details(self) -> None:
        """When publisher is not set, details.publisher is absent."""
        hub = WampHub(realm="realm1")
        ws, session = await make_subscribed_session(hub, ["com.topic"])
        try:
            await session.publish("com.topic", args=["data"])

            event = find_msg_by_type(ws, WampMessageType.EVENT)
            assert event is not None
            details = event[3]
            assert "publisher" not in details
        finally:
            await cleanup_session(session)

    async def test_publish_multiple_topics(self) -> None:
        """Session subscribed to multiple topics receives events for each."""
        hub = WampHub(realm="realm1")
        ws, session = await make_subscribed_session(hub, ["com.a", "com.b"])
        try:
            await session.publish("com.a", args=[1])
            await session.publish("com.b", args=[2])

            events = find_all_msgs_by_type(ws, WampMessageType.EVENT)
            assert len(events) == 2

            sub_a = session.subscription_uris.get("com.a")
            sub_b = session.subscription_uris.get("com.b")
            assert events[0][1] == sub_a
            assert events[1][1] == sub_b
            assert events[0][4] == [1]
            assert events[1][4] == [2]
        finally:
            await cleanup_session(session)


# ---------------------------------------------------------------------------
# hub.publish_to_all() tests
# ---------------------------------------------------------------------------


class TestPublishToAll:
    """hub.publish_to_all(topic, args, kwargs) publishes to all subscribed sessions."""

    async def test_publish_to_all_multiple_sessions(self) -> None:
        """publish_to_all sends EVENT to all sessions subscribed to the topic."""
        hub = WampHub(realm="realm1")
        ws1, session1 = await make_subscribed_session(hub, ["com.broadcast"])
        ws2, session2 = await make_subscribed_session(hub, ["com.broadcast"])
        try:
            await hub.publish_to_all("com.broadcast", args=["hello everyone"])

            event1 = find_msg_by_type(ws1, WampMessageType.EVENT)
            event2 = find_msg_by_type(ws2, WampMessageType.EVENT)
            assert event1 is not None
            assert event2 is not None
            assert event1[4] == ["hello everyone"]
            assert event2[4] == ["hello everyone"]

            # Publication IDs should be different (per-session counters)
            assert event1[2] != event2[2] or event1[1] != event2[1]
        finally:
            await cleanup_session(session1)
            await cleanup_session(session2)

    async def test_publish_to_all_skips_unsubscribed(self) -> None:
        """publish_to_all skips sessions not subscribed to the topic."""
        hub = WampHub(realm="realm1")
        ws1, session1 = await make_subscribed_session(hub, ["com.target"])
        ws2, session2 = await make_subscribed_session(hub, ["com.other"])
        try:
            await hub.publish_to_all("com.target", args=["only for target"])

            event1 = find_msg_by_type(ws1, WampMessageType.EVENT)
            event2 = find_msg_by_type(ws2, WampMessageType.EVENT)
            assert event1 is not None
            assert event1[4] == ["only for target"]
            # session2 is subscribed to com.other, not com.target
            assert event2 is None
        finally:
            await cleanup_session(session1)
            await cleanup_session(session2)

    async def test_publish_to_all_with_kwargs(self) -> None:
        """publish_to_all passes kwargs through to EVENT."""
        hub = WampHub(realm="realm1")
        ws1, session1 = await make_subscribed_session(hub, ["com.topic"])
        try:
            await hub.publish_to_all("com.topic", args=[1], kwargs={"key": "val"})

            event = find_msg_by_type(ws1, WampMessageType.EVENT)
            assert event is not None
            assert event[4] == [1]
            assert event[5] == {"key": "val"}
        finally:
            await cleanup_session(session1)

    async def test_publish_to_all_with_publisher(self) -> None:
        """publish_to_all passes publisher identification to all events."""
        hub = WampHub(realm="realm1")
        ws1, session1 = await make_subscribed_session(hub, ["com.topic"])
        ws2, session2 = await make_subscribed_session(hub, ["com.topic"])
        try:
            await hub.publish_to_all("com.topic", args=["data"], publisher=99999)

            event1 = find_msg_by_type(ws1, WampMessageType.EVENT)
            event2 = find_msg_by_type(ws2, WampMessageType.EVENT)
            assert event1 is not None
            assert event2 is not None
            assert event1[3].get("publisher") == 99999
            assert event2[3].get("publisher") == 99999
        finally:
            await cleanup_session(session1)
            await cleanup_session(session2)

    async def test_publish_to_all_no_sessions(self) -> None:
        """publish_to_all with no active sessions does nothing (no error)."""
        hub = WampHub(realm="realm1")
        # No sessions connected — should not raise
        await hub.publish_to_all("com.topic", args=["nobody listening"])

    async def test_publish_to_all_no_subscribers(self) -> None:
        """publish_to_all to a topic with no subscribers is a no-op."""
        hub = WampHub(realm="realm1")
        ws1, session1 = await make_subscribed_session(hub, ["com.other"])
        try:
            await hub.publish_to_all("com.unsubscribed", args=["ignored"])

            event = find_msg_by_type(ws1, WampMessageType.EVENT)
            assert event is None
        finally:
            await cleanup_session(session1)


# ---------------------------------------------------------------------------
# FastAPI integration tests
# ---------------------------------------------------------------------------


class TestFastAPIPublishIntegration:
    """Integration tests using FastAPI test client for server-to-client publish."""

    def _make_app(self, hub: WampHub) -> FastAPI:
        """Create a FastAPI app with a /ws endpoint."""
        app = FastAPI()

        @app.websocket("/ws")
        async def wamp_endpoint(websocket: FastAPIWebSocket) -> None:
            await hub.handle_websocket(websocket)

        return app

    def test_publish_event_to_subscribed_client(self) -> None:
        """FastAPI: server publishes event to subscribed client."""
        hub = WampHub(realm="realm1")
        app = self._make_app(hub)
        published_ok = False

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            nonlocal published_ok

            # Wait a moment for subscription to be processed
            # (on_session_open fires before message loop starts)
            async def _delayed_publish() -> None:
                nonlocal published_ok
                # Small delay to let message loop process SUBSCRIBE
                await asyncio.sleep(0.1)
                await session.publish("com.events.test", args=["server push"])
                published_ok = True

            asyncio.create_task(_delayed_publish())

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                # Handshake
                hello: list[Any] = [
                    WampMessageType.HELLO,
                    "realm1",
                    {"roles": {"subscriber": {}}},
                ]
                ws.send_json(hello)
                welcome: list[Any] = ws.receive_json()
                assert welcome[0] == WampMessageType.WELCOME

                # Subscribe
                subscribe: list[Any] = [
                    WampMessageType.SUBSCRIBE,
                    1,
                    {},
                    "com.events.test",
                ]
                ws.send_json(subscribe)
                subscribed: list[Any] = ws.receive_json()
                assert subscribed[0] == WampMessageType.SUBSCRIBED
                sub_id = subscribed[2]

                # Wait for the EVENT pushed by the server
                event: list[Any] = ws.receive_json()
                assert event[0] == WampMessageType.EVENT
                assert event[1] == sub_id  # subscription_id matches
                assert isinstance(event[2], int)  # publication_id
                assert event[2] > 0
                assert event[4] == ["server push"]

                # Goodbye
                goodbye: list[Any] = [
                    WampMessageType.GOODBYE,
                    {},
                    "wamp.close.normal",
                ]
                ws.send_json(goodbye)
                goodbye_reply: list[Any] = ws.receive_json()
                assert goodbye_reply[0] == WampMessageType.GOODBYE

    def test_publish_to_all_fastapi(self) -> None:
        """FastAPI: publish_to_all sends EVENT to multiple connected clients."""
        hub = WampHub(realm="realm1")
        app = self._make_app(hub)

        # We'll use a single client for simplicity (publish_to_all iterates all sessions)
        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            async def _delayed_publish() -> None:
                await asyncio.sleep(0.1)
                await hub.publish_to_all("com.broadcast", args=["broadcast msg"])

            asyncio.create_task(_delayed_publish())

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                # Handshake
                hello: list[Any] = [
                    WampMessageType.HELLO,
                    "realm1",
                    {"roles": {"subscriber": {}}},
                ]
                ws.send_json(hello)
                welcome: list[Any] = ws.receive_json()
                assert welcome[0] == WampMessageType.WELCOME

                # Subscribe
                subscribe: list[Any] = [
                    WampMessageType.SUBSCRIBE,
                    1,
                    {},
                    "com.broadcast",
                ]
                ws.send_json(subscribe)
                subscribed: list[Any] = ws.receive_json()
                assert subscribed[0] == WampMessageType.SUBSCRIBED

                # Wait for EVENT from publish_to_all
                event: list[Any] = ws.receive_json()
                assert event[0] == WampMessageType.EVENT
                assert event[4] == ["broadcast msg"]

                # Goodbye
                goodbye: list[Any] = [
                    WampMessageType.GOODBYE,
                    {},
                    "wamp.close.normal",
                ]
                ws.send_json(goodbye)
                ws.receive_json()  # GOODBYE reply

    def test_publish_unsubscribed_noop_fastapi(self) -> None:
        """FastAPI: publishing to unsubscribed client is a no-op."""
        hub = WampHub(realm="realm1")
        app = self._make_app(hub)
        publish_completed = False

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            nonlocal publish_completed
            # Publish to a topic the client hasn't subscribed to
            await session.publish("com.unsubscribed.topic", args=["ignored"])
            publish_completed = True

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                # Handshake
                hello: list[Any] = [
                    WampMessageType.HELLO,
                    "realm1",
                    {"roles": {}},
                ]
                ws.send_json(hello)
                welcome: list[Any] = ws.receive_json()
                assert welcome[0] == WampMessageType.WELCOME

                # The publish should have completed (no-op since not subscribed)
                assert publish_completed

                # Goodbye — should receive only the GOODBYE reply (no EVENT)
                goodbye: list[Any] = [
                    WampMessageType.GOODBYE,
                    {},
                    "wamp.close.normal",
                ]
                ws.send_json(goodbye)
                goodbye_reply: list[Any] = ws.receive_json()
                assert goodbye_reply[0] == WampMessageType.GOODBYE
