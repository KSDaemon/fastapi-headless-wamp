"""Tests for PubSub: client SUBSCRIBE/UNSUBSCRIBE handling (US-017).

Covers:
- Client SUBSCRIBE -> server stores in session subscription map, sends SUBSCRIBED
- Client UNSUBSCRIBE -> server removes from map, sends UNSUBSCRIBED
- UNSUBSCRIBE for non-existent subscription -> ERROR wamp.error.no_such_subscription
- Subscriptions are per-session (isolated, not shared across clients)
- All subscriptions cleaned up on session disconnect
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
from fastapi_headless_wamp.protocol import (
    WAMP_ERROR_NO_SUCH_SUBSCRIPTION,
    WampMessageType,
)
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
# SUBSCRIBE tests
# ---------------------------------------------------------------------------


class TestSubscribe:
    """Client sends SUBSCRIBE -> server sends SUBSCRIBED."""

    async def test_subscribe_success(self) -> None:
        """SUBSCRIBE for a topic returns SUBSCRIBED with a subscription ID."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        subscribe: list[Any] = [WampMessageType.SUBSCRIBE, 1, {}, "com.example.topic"]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(subscribe))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Find SUBSCRIBED message
        subscribed = find_msg_by_type(ws, WampMessageType.SUBSCRIBED)
        assert subscribed is not None
        assert subscribed[0] == WampMessageType.SUBSCRIBED
        assert subscribed[1] == 1  # echoes back the request_id
        assert isinstance(subscribed[2], int)
        assert subscribed[2] > 0  # subscription_id

    async def test_subscribe_stores_in_session_maps(self) -> None:
        """SUBSCRIBE stores the topic in session's subscriptions and subscription_uris."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        subscribe: list[Any] = [WampMessageType.SUBSCRIBE, 1, {}, "com.example.topic"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(subscribe))

        # Capture subscription state inside the hooked loop before cleanup
        session_ref: list[WampSession] = []
        sub_count: list[int] = []
        sub_topic: list[str] = []
        sub_reverse: list[int] = []

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            session_ref.append(session)

        original_loop = hub._message_loop  # type: ignore[attr-defined]

        async def hooked_loop(session: WampSession) -> None:
            # Process subscribe message
            msg = await session.receive_message()
            await hub._handle_subscribe(session, msg)  # type: ignore[attr-defined]

            # Capture subscription state before cleanup
            sub_count.append(len(session.subscriptions))
            if session.subscriptions:
                sub_id = list(session.subscriptions.keys())[0]
                sub_topic.append(session.subscriptions[sub_id])
                sub_reverse.append(session.subscription_uris.get("com.example.topic", -1))

            # Now process goodbye
            goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
            ws.enqueue_text(json.dumps(goodbye))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Verify subscription state was captured correctly
        assert sub_count == [1]
        assert sub_topic == ["com.example.topic"]
        assert len(sub_reverse) == 1
        assert sub_reverse[0] > 0  # valid subscription_id

    async def test_subscribe_multiple_topics(self) -> None:
        """Multiple SUBSCRIBE messages get unique subscription IDs."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        sub1: list[Any] = [WampMessageType.SUBSCRIBE, 1, {}, "com.topic.one"]
        sub2: list[Any] = [WampMessageType.SUBSCRIBE, 2, {}, "com.topic.two"]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(sub1))
        ws.enqueue_text(json.dumps(sub2))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        subscribed_msgs = find_all_msgs_by_type(ws, WampMessageType.SUBSCRIBED)
        assert len(subscribed_msgs) == 2

        # Request IDs should match
        assert subscribed_msgs[0][1] == 1
        assert subscribed_msgs[1][1] == 2

        # Subscription IDs should be unique
        sub_id1 = subscribed_msgs[0][2]
        sub_id2 = subscribed_msgs[1][2]
        assert sub_id1 != sub_id2

    async def test_subscribe_request_id_echoed(self) -> None:
        """SUBSCRIBED echoes back the request_id from SUBSCRIBE."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        subscribe: list[Any] = [WampMessageType.SUBSCRIBE, 42, {}, "com.example.topic"]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(subscribe))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        subscribed = find_msg_by_type(ws, WampMessageType.SUBSCRIBED)
        assert subscribed is not None
        assert subscribed[1] == 42

    async def test_subscribe_unique_ids_across_sessions(self) -> None:
        """Subscription IDs are unique across sessions (global counter)."""
        hub = WampHub(realm="realm1")
        subscription_ids: list[int] = []

        for _ in range(2):
            ws = MockWebSocket(subprotocols=["wamp.2.json"])
            hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
            subscribe: list[Any] = [WampMessageType.SUBSCRIBE, 1, {}, "com.topic"]
            goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
            ws.enqueue_text(json.dumps(hello))
            ws.enqueue_text(json.dumps(subscribe))
            ws.enqueue_text(json.dumps(goodbye))

            await hub.handle_websocket(ws)  # type: ignore[arg-type]

            subscribed = find_msg_by_type(ws, WampMessageType.SUBSCRIBED)
            assert subscribed is not None
            subscription_ids.append(subscribed[2])

        assert subscription_ids[0] != subscription_ids[1]


# ---------------------------------------------------------------------------
# UNSUBSCRIBE tests
# ---------------------------------------------------------------------------


class TestUnsubscribe:
    """Client sends UNSUBSCRIBE -> server sends UNSUBSCRIBED."""

    async def test_unsubscribe_success(self) -> None:
        """UNSUBSCRIBE for a valid subscription returns UNSUBSCRIBED."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        subscribe: list[Any] = [WampMessageType.SUBSCRIBE, 1, {}, "com.example.topic"]
        # We'll fill in the unsubscribe after we know the subscription_id
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(subscribe))

        # Hook the loop to inspect and continue
        original_loop = hub._message_loop  # type: ignore[attr-defined]

        async def hooked_loop(session: WampSession) -> None:
            # Process subscribe
            msg = await session.receive_message()
            await hub._handle_subscribe(session, msg)  # type: ignore[attr-defined]

            # Get the subscription_id from the SUBSCRIBED message
            subscribed = find_msg_by_type(ws, WampMessageType.SUBSCRIBED)
            assert subscribed is not None
            sub_id = subscribed[2]

            # Now enqueue unsubscribe and goodbye
            unsub: list[Any] = [WampMessageType.UNSUBSCRIBE, 2, sub_id]
            goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
            ws.enqueue_text(json.dumps(unsub))
            ws.enqueue_text(json.dumps(goodbye))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Find UNSUBSCRIBED message
        unsubscribed = find_msg_by_type(ws, WampMessageType.UNSUBSCRIBED)
        assert unsubscribed is not None
        assert unsubscribed[0] == WampMessageType.UNSUBSCRIBED
        assert unsubscribed[1] == 2  # echoes back the request_id

    async def test_unsubscribe_removes_from_maps(self) -> None:
        """UNSUBSCRIBE removes the topic from session's maps."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        subscribe: list[Any] = [WampMessageType.SUBSCRIBE, 1, {}, "com.example.topic"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(subscribe))

        session_ref: list[WampSession] = []

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            session_ref.append(session)

        original_loop = hub._message_loop  # type: ignore[attr-defined]

        async def hooked_loop(session: WampSession) -> None:
            # Process subscribe
            msg = await session.receive_message()
            await hub._handle_subscribe(session, msg)  # type: ignore[attr-defined]

            # Verify it's in the maps
            assert len(session.subscriptions) == 1
            sub_id = list(session.subscriptions.keys())[0]

            # Now unsubscribe
            unsub: list[Any] = [WampMessageType.UNSUBSCRIBE, 2, sub_id]
            ws.enqueue_text(json.dumps(unsub))
            msg2 = await session.receive_message()
            await hub._handle_unsubscribe(session, msg2)  # type: ignore[attr-defined]

            # Verify it's removed
            assert len(session.subscriptions) == 0
            assert len(session.subscription_uris) == 0

            # Now goodbye
            goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
            ws.enqueue_text(json.dumps(goodbye))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

    async def test_unsubscribe_nonexistent_returns_error(self) -> None:
        """UNSUBSCRIBE for non-existent subscription returns ERROR."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        unsub: list[Any] = [WampMessageType.UNSUBSCRIBE, 1, 999]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(unsub))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Find ERROR message
        error = find_msg_by_type(ws, WampMessageType.ERROR)
        assert error is not None
        assert error[0] == WampMessageType.ERROR
        assert error[1] == WampMessageType.UNSUBSCRIBE
        assert error[2] == 1  # request_id
        assert error[4] == WAMP_ERROR_NO_SUCH_SUBSCRIPTION

    async def test_double_unsubscribe_returns_error(self) -> None:
        """Unsubscribing twice from the same topic returns ERROR the second time."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        subscribe: list[Any] = [WampMessageType.SUBSCRIBE, 1, {}, "com.topic"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(subscribe))

        original_loop = hub._message_loop  # type: ignore[attr-defined]

        async def hooked_loop(session: WampSession) -> None:
            # Process subscribe
            msg = await session.receive_message()
            await hub._handle_subscribe(session, msg)  # type: ignore[attr-defined]

            subscribed = find_msg_by_type(ws, WampMessageType.SUBSCRIBED)
            assert subscribed is not None
            sub_id = subscribed[2]

            # First unsubscribe (should succeed)
            unsub1: list[Any] = [WampMessageType.UNSUBSCRIBE, 2, sub_id]
            ws.enqueue_text(json.dumps(unsub1))
            msg2 = await session.receive_message()
            await hub._handle_unsubscribe(session, msg2)  # type: ignore[attr-defined]

            # Second unsubscribe (should fail)
            unsub2: list[Any] = [WampMessageType.UNSUBSCRIBE, 3, sub_id]
            ws.enqueue_text(json.dumps(unsub2))
            msg3 = await session.receive_message()
            await hub._handle_unsubscribe(session, msg3)  # type: ignore[attr-defined]

            goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
            ws.enqueue_text(json.dumps(goodbye))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Should have UNSUBSCRIBED (for first) and ERROR (for second)
        unsubscribed = find_msg_by_type(ws, WampMessageType.UNSUBSCRIBED)
        assert unsubscribed is not None

        error = find_msg_by_type(ws, WampMessageType.ERROR)
        assert error is not None
        assert error[4] == WAMP_ERROR_NO_SUCH_SUBSCRIPTION


# ---------------------------------------------------------------------------
# Per-session isolation
# ---------------------------------------------------------------------------


class TestSubscriptionIsolation:
    """Subscriptions are per-session and do not leak across clients."""

    async def test_independent_subscriptions_across_sessions(self) -> None:
        """Multiple clients can subscribe to the same topic independently."""
        hub = WampHub(realm="realm1")
        sessions_data: list[dict[str, Any]] = []

        for _ in range(2):
            ws = MockWebSocket(subprotocols=["wamp.2.json"])
            hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
            subscribe: list[Any] = [
                WampMessageType.SUBSCRIBE,
                1,
                {},
                "com.shared.topic",
            ]
            goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
            ws.enqueue_text(json.dumps(hello))
            ws.enqueue_text(json.dumps(subscribe))
            ws.enqueue_text(json.dumps(goodbye))

            await hub.handle_websocket(ws)  # type: ignore[arg-type]

            subscribed = find_msg_by_type(ws, WampMessageType.SUBSCRIBED)
            assert subscribed is not None
            sessions_data.append({"sub_id": subscribed[2]})

        # Subscription IDs should be different (global counter)
        assert sessions_data[0]["sub_id"] != sessions_data[1]["sub_id"]

    async def test_unsubscribe_isolation(self) -> None:
        """Unsubscribing in one session does not affect another session's subscriptions."""
        hub = WampHub(realm="realm1")
        ws1 = MockWebSocket(subprotocols=["wamp.2.json"])
        ws2 = MockWebSocket(subprotocols=["wamp.2.json"])

        session_refs: list[WampSession] = []

        @hub.on_session_open
        async def on_open(session: WampSession) -> None:
            session_refs.append(session)

        # Session 1: subscribe and unsubscribe
        hello1: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        sub1: list[Any] = [WampMessageType.SUBSCRIBE, 1, {}, "com.topic"]
        ws1.enqueue_text(json.dumps(hello1))
        ws1.enqueue_text(json.dumps(sub1))

        original_loop = hub._message_loop  # type: ignore[attr-defined]
        first_session = True

        async def hooked_loop_1(session: WampSession) -> None:
            nonlocal first_session
            if first_session:
                first_session = False
                # Process subscribe
                msg = await session.receive_message()
                await hub._handle_subscribe(session, msg)  # type: ignore[attr-defined]

                subscribed = find_msg_by_type(ws1, WampMessageType.SUBSCRIBED)
                assert subscribed is not None
                sub_id = subscribed[2]

                # Unsubscribe
                unsub: list[Any] = [WampMessageType.UNSUBSCRIBE, 2, sub_id]
                ws1.enqueue_text(json.dumps(unsub))
                msg2 = await session.receive_message()
                await hub._handle_unsubscribe(session, msg2)  # type: ignore[attr-defined]

                # Assert session1 has no subscriptions
                assert len(session.subscriptions) == 0

                goodbye: list[Any] = [
                    WampMessageType.GOODBYE,
                    {},
                    "wamp.close.normal",
                ]
                ws1.enqueue_text(json.dumps(goodbye))
                await original_loop(session)
            else:
                await original_loop(session)

        hub._message_loop = hooked_loop_1  # type: ignore[method-assign]

        await hub.handle_websocket(ws1)  # type: ignore[arg-type]

        # Session 2: subscribe to same topic (should work fine)
        hub._message_loop = original_loop  # type: ignore[method-assign]
        hello2: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        sub2: list[Any] = [WampMessageType.SUBSCRIBE, 1, {}, "com.topic"]
        goodbye2: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws2.enqueue_text(json.dumps(hello2))
        ws2.enqueue_text(json.dumps(sub2))
        ws2.enqueue_text(json.dumps(goodbye2))

        await hub.handle_websocket(ws2)  # type: ignore[arg-type]

        subscribed2 = find_msg_by_type(ws2, WampMessageType.SUBSCRIBED)
        assert subscribed2 is not None


# ---------------------------------------------------------------------------
# Cleanup on disconnect
# ---------------------------------------------------------------------------


class TestSubscriptionCleanup:
    """Subscriptions are cleaned up on session disconnect."""

    async def test_subscriptions_cleared_on_goodbye(self) -> None:
        """Subscriptions are cleared when the client sends GOODBYE."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])
        session_subscriptions_after_close: list[int] = []

        @hub.on_session_close
        async def on_close(session: WampSession) -> None:
            # Before cleanup(), session still has subscriptions
            session_subscriptions_after_close.append(len(session.subscriptions))

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        subscribe: list[Any] = [WampMessageType.SUBSCRIBE, 1, {}, "com.example.topic"]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.normal"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(subscribe))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # on_session_close fires before cleanup, so it should see the subscription
        assert session_subscriptions_after_close == [1]

    async def test_subscriptions_cleared_on_disconnect(self) -> None:
        """Subscriptions are cleaned up when WebSocket disconnects unexpectedly."""
        from starlette.websockets import WebSocketDisconnect

        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        subscribe: list[Any] = [WampMessageType.SUBSCRIBE, 1, {}, "com.example.topic"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(subscribe))

        async def hooked_loop(session: WampSession) -> None:
            # Process subscribe first
            msg = await session.receive_message()
            await hub._handle_subscribe(session, msg)  # type: ignore[attr-defined]

            # Verify subscription is there
            assert len(session.subscriptions) == 1

            # Simulate disconnect
            raise WebSocketDisconnect()

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Hub should have no active sessions
        assert hub.session_count == 0


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

    def test_subscribe_fastapi(self) -> None:
        """FastAPI: client subscribes to a topic and receives SUBSCRIBED."""
        hub = WampHub(realm="realm1")
        app = self._make_app(hub)

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
                    "com.example.topic",
                ]
                ws.send_json(subscribe)
                subscribed: list[Any] = ws.receive_json()
                assert subscribed[0] == WampMessageType.SUBSCRIBED
                assert subscribed[1] == 1  # request_id echoed
                assert isinstance(subscribed[2], int)
                assert subscribed[2] > 0

                # Goodbye
                goodbye: list[Any] = [
                    WampMessageType.GOODBYE,
                    {},
                    "wamp.close.normal",
                ]
                ws.send_json(goodbye)
                goodbye_reply: list[Any] = ws.receive_json()
                assert goodbye_reply[0] == WampMessageType.GOODBYE

    def test_unsubscribe_fastapi(self) -> None:
        """FastAPI: client subscribes then unsubscribes, gets UNSUBSCRIBED."""
        hub = WampHub(realm="realm1")
        app = self._make_app(hub)

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
                    "com.example.topic",
                ]
                ws.send_json(subscribe)
                subscribed: list[Any] = ws.receive_json()
                assert subscribed[0] == WampMessageType.SUBSCRIBED
                sub_id = subscribed[2]

                # Unsubscribe
                unsubscribe: list[Any] = [
                    WampMessageType.UNSUBSCRIBE,
                    2,
                    sub_id,
                ]
                ws.send_json(unsubscribe)
                unsubscribed: list[Any] = ws.receive_json()
                assert unsubscribed[0] == WampMessageType.UNSUBSCRIBED
                assert unsubscribed[1] == 2  # request_id echoed

                # Goodbye
                goodbye: list[Any] = [
                    WampMessageType.GOODBYE,
                    {},
                    "wamp.close.normal",
                ]
                ws.send_json(goodbye)
                goodbye_reply: list[Any] = ws.receive_json()
                assert goodbye_reply[0] == WampMessageType.GOODBYE

    def test_nonexistent_unsubscribe_fastapi(self) -> None:
        """FastAPI: UNSUBSCRIBE for non-existent subscription returns ERROR."""
        hub = WampHub(realm="realm1")
        app = self._make_app(hub)

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

                # Unsubscribe for non-existent subscription
                unsubscribe: list[Any] = [
                    WampMessageType.UNSUBSCRIBE,
                    1,
                    999,
                ]
                ws.send_json(unsubscribe)
                error: list[Any] = ws.receive_json()
                assert error[0] == WampMessageType.ERROR
                assert error[1] == WampMessageType.UNSUBSCRIBE
                assert error[2] == 1
                assert error[4] == WAMP_ERROR_NO_SUCH_SUBSCRIPTION

                # Goodbye
                goodbye: list[Any] = [
                    WampMessageType.GOODBYE,
                    {},
                    "wamp.close.normal",
                ]
                ws.send_json(goodbye)
                goodbye_reply: list[Any] = ws.receive_json()
                assert goodbye_reply[0] == WampMessageType.GOODBYE

    def test_multiple_subscriptions_fastapi(self) -> None:
        """FastAPI: multiple subscriptions get unique IDs."""
        hub = WampHub(realm="realm1")
        app = self._make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                # Handshake
                hello: list[Any] = [
                    WampMessageType.HELLO,
                    "realm1",
                    {"roles": {}},
                ]
                ws.send_json(hello)
                ws.receive_json()  # WELCOME

                # Subscribe to two topics
                sub1: list[Any] = [
                    WampMessageType.SUBSCRIBE,
                    1,
                    {},
                    "com.topic.one",
                ]
                sub2: list[Any] = [
                    WampMessageType.SUBSCRIBE,
                    2,
                    {},
                    "com.topic.two",
                ]
                ws.send_json(sub1)
                subscribed1: list[Any] = ws.receive_json()
                ws.send_json(sub2)
                subscribed2: list[Any] = ws.receive_json()

                assert subscribed1[0] == WampMessageType.SUBSCRIBED
                assert subscribed2[0] == WampMessageType.SUBSCRIBED
                assert subscribed1[2] != subscribed2[2]  # unique IDs

                # Goodbye
                goodbye: list[Any] = [
                    WampMessageType.GOODBYE,
                    {},
                    "wamp.close.normal",
                ]
                ws.send_json(goodbye)
                ws.receive_json()  # GOODBYE reply
