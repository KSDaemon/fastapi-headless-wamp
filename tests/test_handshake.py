"""Tests for WAMP session handshake (US-006).

Covers:
- Subprotocol negotiation
- Successful HELLO -> WELCOME handshake
- GOODBYE flow
- ABORT on invalid first message
- Session ID uniqueness
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

from fastapi_headless_wamp.hub import WampHub
from fastapi_headless_wamp.protocol import (
    WAMP_ERROR_CLOSE_REALM,
    WAMP_ERROR_GOODBYE_AND_OUT,
    WampMessageType,
)
from fastapi_headless_wamp.serializers import JsonSerializer
from fastapi_headless_wamp.session import (
    BROKER_FEATURES,
    DEALER_FEATURES,
    WampSession,
    negotiate_subprotocol,
)

# ---------------------------------------------------------------------------
# Mock WebSocket for handshake tests
# ---------------------------------------------------------------------------


class MockWebSocket:
    """A mock that simulates Starlette's WebSocket for handshake testing."""

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


def make_session(
    ws: MockWebSocket | None = None,
    subprotocols: list[str] | None = None,
) -> tuple[WampSession, MockWebSocket]:
    """Create a WampSession with a mock WebSocket and JSON serializer."""
    if ws is None:
        ws = MockWebSocket(subprotocols)
    serializer = JsonSerializer()
    session = WampSession(ws, serializer)  # type: ignore[arg-type]
    return session, ws


def parse_sent(ws: MockWebSocket, index: int = 0) -> list[Any]:
    """Parse the JSON message sent by the session at the given index."""
    result: list[Any] = json.loads(ws.sent_texts[index])
    return result


# ---------------------------------------------------------------------------
# Subprotocol negotiation tests
# ---------------------------------------------------------------------------


class TestSubprotocolNegotiation:
    """Tests for the negotiate_subprotocol function."""

    def test_json_subprotocol_matched(self) -> None:
        result = negotiate_subprotocol(["wamp.2.json"])
        assert result is not None
        subproto, serializer = result
        assert subproto == "wamp.2.json"
        assert serializer.protocol == "json"
        assert serializer.is_binary is False

    def test_first_matching_subprotocol_selected(self) -> None:
        result = negotiate_subprotocol(["wamp.2.msgpack", "wamp.2.json"])
        assert result is not None
        subproto, _serializer = result
        # Both are registered; the first match from the client's list wins
        assert subproto == "wamp.2.msgpack"

    def test_no_matching_subprotocol(self) -> None:
        result = negotiate_subprotocol(["wamp.2.flatbuffers", "wamp.2.protobuf"])
        assert result is None

    def test_empty_subprotocol_list(self) -> None:
        result = negotiate_subprotocol([])
        assert result is None

    def test_non_wamp_subprotocol_ignored(self) -> None:
        result = negotiate_subprotocol(["graphql-ws", "some-other"])
        assert result is None


# ---------------------------------------------------------------------------
# Successful handshake tests
# ---------------------------------------------------------------------------


class TestSuccessfulHandshake:
    """Tests for the HELLO -> WELCOME handshake."""

    async def test_basic_handshake(self) -> None:
        session, ws = make_session()
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {"caller": {}}}]
        ws.enqueue_text(json.dumps(hello))

        result = await session.perform_handshake("realm1")

        assert result is True
        assert session.is_open is True
        assert session.realm == "realm1"
        assert 1 <= session.session_id <= 2**53

    async def test_welcome_message_structure(self) -> None:
        session, ws = make_session()
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        ws.enqueue_text(json.dumps(hello))

        await session.perform_handshake("realm1")

        # Verify WELCOME was sent
        assert len(ws.sent_texts) == 1
        welcome = parse_sent(ws, 0)

        assert welcome[0] == WampMessageType.WELCOME
        assert welcome[1] == session.session_id
        assert isinstance(welcome[2], dict)

        # Check roles in details
        roles = welcome[2]["roles"]
        assert "dealer" in roles
        assert "broker" in roles
        assert roles["dealer"]["features"] == DEALER_FEATURES
        assert roles["broker"]["features"] == BROKER_FEATURES

    async def test_dealer_features(self) -> None:
        """Verify dealer features include required capabilities."""
        assert DEALER_FEATURES["progressive_call_results"] is True
        assert DEALER_FEATURES["call_canceling"] is True
        assert DEALER_FEATURES["caller_identification"] is True
        assert DEALER_FEATURES["call_timeout"] is True

    async def test_broker_features(self) -> None:
        """Verify broker features include required capabilities."""
        assert BROKER_FEATURES["publisher_identification"] is True
        assert BROKER_FEATURES["publisher_exclusion"] is True
        assert BROKER_FEATURES["subscriber_blackwhite_listing"] is True


# ---------------------------------------------------------------------------
# Session ID uniqueness tests
# ---------------------------------------------------------------------------


class TestSessionIdUniqueness:
    """Tests that session IDs are unique across active sessions."""

    async def test_unique_session_id_with_existing(self) -> None:
        session, ws = make_session()
        existing: set[int] = {100, 200, 300}
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {}]
        ws.enqueue_text(json.dumps(hello))

        await session.perform_handshake("realm1", existing_ids=existing)

        assert session.session_id not in existing
        assert 1 <= session.session_id <= 2**53

    async def test_session_id_avoids_collisions(self) -> None:
        """generate_session_id retries when collision detected."""
        session, _ws = make_session()
        existing: set[int] = set()

        # Generate several IDs and verify all unique
        generated: list[int] = []
        for _ in range(10):
            sid = session.generate_session_id(existing)
            assert sid not in existing
            existing.add(sid)
            generated.append(sid)

        assert len(set(generated)) == 10


# ---------------------------------------------------------------------------
# GOODBYE flow tests
# ---------------------------------------------------------------------------


class TestGoodbyeFlow:
    """Tests for GOODBYE handling."""

    async def test_goodbye_response(self) -> None:
        session, ws = make_session()
        session.session_id = 42
        session.is_open = True

        goodbye_msg: list[Any] = [
            WampMessageType.GOODBYE,
            {},
            "wamp.close.close_realm",
        ]
        await session.handle_goodbye(goodbye_msg)

        # Verify GOODBYE reply sent
        assert len(ws.sent_texts) == 1
        reply = parse_sent(ws, 0)

        assert reply[0] == WampMessageType.GOODBYE
        assert reply[1] == {}
        assert reply[2] == WAMP_ERROR_GOODBYE_AND_OUT

    async def test_goodbye_closes_session(self) -> None:
        session, _ws = make_session()
        session.session_id = 42
        session.is_open = True

        goodbye_msg: list[Any] = [
            WampMessageType.GOODBYE,
            {"message": "closing"},
            "wamp.close.close_realm",
        ]
        await session.handle_goodbye(goodbye_msg)

        assert session.is_open is False


# ---------------------------------------------------------------------------
# ABORT on invalid first message tests
# ---------------------------------------------------------------------------


class TestAbortOnInvalidFirstMessage:
    """Tests for ABORT when first message is not HELLO."""

    async def test_abort_on_call_as_first_message(self) -> None:
        """If client sends CALL instead of HELLO, server ABORTs."""
        session, ws = make_session()
        call_msg: list[Any] = [WampMessageType.CALL, 1, {}, "com.add"]
        ws.enqueue_text(json.dumps(call_msg))

        result = await session.perform_handshake("realm1")

        assert result is False
        assert session.is_open is False

        # Verify ABORT was sent
        assert len(ws.sent_texts) == 1
        abort = parse_sent(ws, 0)
        assert abort[0] == WampMessageType.ABORT
        assert isinstance(abort[1], dict)
        assert isinstance(abort[2], str)

    async def test_abort_on_goodbye_as_first_message(self) -> None:
        """If client sends GOODBYE instead of HELLO, server ABORTs."""
        session, ws = make_session()
        goodbye_msg: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.close_realm"]
        ws.enqueue_text(json.dumps(goodbye_msg))

        result = await session.perform_handshake("realm1")

        assert result is False
        assert len(ws.sent_texts) == 1
        abort = parse_sent(ws, 0)
        assert abort[0] == WampMessageType.ABORT

    async def test_abort_on_empty_message(self) -> None:
        """If client sends an empty array, server ABORTs."""
        session, ws = make_session()
        ws.enqueue_text(json.dumps([]))

        result = await session.perform_handshake("realm1")

        assert result is False
        assert len(ws.sent_texts) == 1
        abort = parse_sent(ws, 0)
        assert abort[0] == WampMessageType.ABORT

    async def test_abort_on_wrong_realm(self) -> None:
        """If client sends HELLO with wrong realm, server ABORTs."""
        session, ws = make_session()
        hello: list[Any] = [WampMessageType.HELLO, "wrong_realm", {}]
        ws.enqueue_text(json.dumps(hello))

        result = await session.perform_handshake("realm1")

        assert result is False
        assert session.is_open is False

        abort = parse_sent(ws, 0)
        assert abort[0] == WampMessageType.ABORT
        assert abort[2] == WAMP_ERROR_CLOSE_REALM

    async def test_abort_on_malformed_hello(self) -> None:
        """If HELLO has wrong types, server ABORTs."""
        session, ws = make_session()
        # HELLO with int realm instead of str
        bad_hello: list[Any] = [WampMessageType.HELLO, 12345, {}]
        ws.enqueue_text(json.dumps(bad_hello))

        result = await session.perform_handshake("realm1")

        assert result is False
        assert len(ws.sent_texts) == 1
        abort = parse_sent(ws, 0)
        assert abort[0] == WampMessageType.ABORT

    async def test_abort_on_truncated_hello(self) -> None:
        """If HELLO is too short, server ABORTs."""
        session, ws = make_session()
        short_hello: list[Any] = [WampMessageType.HELLO]
        ws.enqueue_text(json.dumps(short_hello))

        result = await session.perform_handshake("realm1")

        assert result is False
        assert len(ws.sent_texts) == 1
        abort = parse_sent(ws, 0)
        assert abort[0] == WampMessageType.ABORT


# ---------------------------------------------------------------------------
# Handshake failure on disconnect
# ---------------------------------------------------------------------------


class TestHandshakeDisconnect:
    """Tests for handshake when client disconnects."""

    async def test_handshake_fails_on_receive_error(self) -> None:
        """If receive_message raises an exception, handshake fails."""
        session, _ws = make_session()
        session.receive_message = AsyncMock(  # type: ignore[method-assign]
            side_effect=Exception("connection closed")
        )

        result = await session.perform_handshake("realm1")

        assert result is False
        assert session.is_open is False


# ---------------------------------------------------------------------------
# Integration: Hub handle_websocket (basic)
# ---------------------------------------------------------------------------


class TestHubHandleWebsocket:
    """Basic integration tests for WampHub.handle_websocket."""

    async def test_hub_rejects_no_subprotocol(self) -> None:
        """Hub closes WebSocket if no WAMP subprotocol matches."""

        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["graphql-ws"])

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert ws._closed is True
        assert ws._close_code == 1002
        assert hub.session_count == 0

    async def test_hub_handshake_and_goodbye(self) -> None:
        """Hub completes handshake, session is tracked, GOODBYE removes it."""

        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        # Enqueue HELLO then GOODBYE
        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        goodbye: list[Any] = [WampMessageType.GOODBYE, {}, "wamp.close.close_realm"]
        ws.enqueue_text(json.dumps(hello))
        ws.enqueue_text(json.dumps(goodbye))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Session should have been accepted
        assert ws._accepted is True
        assert ws._accepted_subprotocol == "wamp.2.json"

        # After GOODBYE + cleanup, no sessions should remain
        assert hub.session_count == 0

        # Verify WELCOME and GOODBYE reply were sent
        assert len(ws.sent_texts) == 2
        welcome = json.loads(ws.sent_texts[0])
        goodbye_reply = json.loads(ws.sent_texts[1])

        assert welcome[0] == WampMessageType.WELCOME
        assert goodbye_reply[0] == WampMessageType.GOODBYE
        assert goodbye_reply[2] == WAMP_ERROR_GOODBYE_AND_OUT

    async def test_hub_handshake_abort_on_bad_first_message(self) -> None:
        """Hub aborts if first message is not HELLO."""

        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        call: list[Any] = [WampMessageType.CALL, 1, {}, "com.add"]
        ws.enqueue_text(json.dumps(call))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        assert hub.session_count == 0
        # ABORT should have been sent
        assert len(ws.sent_texts) == 1
        abort = json.loads(ws.sent_texts[0])
        assert abort[0] == WampMessageType.ABORT

    async def test_hub_tracks_multiple_sessions(self) -> None:
        """Hub can track multiple concurrent sessions."""

        hub = WampHub(realm="realm1")

        async def connect_session() -> int:
            ws = MockWebSocket(subprotocols=["wamp.2.json"])
            hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
            ws.enqueue_text(json.dumps(hello))
            # After HELLO, we need to stop the loop - enqueue a GOODBYE
            goodbye: list[Any] = [
                WampMessageType.GOODBYE,
                {},
                "wamp.close.close_realm",
            ]
            ws.enqueue_text(json.dumps(goodbye))
            await hub.handle_websocket(ws)  # type: ignore[arg-type]
            # Return session ID from WELCOME
            welcome: list[Any] = json.loads(ws.sent_texts[0])
            session_id: int = welcome[1]
            return session_id

        # Run two sequential connections and verify unique IDs
        sid1 = await connect_session()
        sid2 = await connect_session()
        assert sid1 != sid2

    async def test_hub_session_tracked_during_connection(self) -> None:
        """Session is accessible in hub.sessions while connected."""

        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        hello: list[Any] = [WampMessageType.HELLO, "realm1", {"roles": {}}]
        ws.enqueue_text(json.dumps(hello))

        # We'll use an event to pause the message loop
        pause_event = asyncio.Event()
        check_event = asyncio.Event()

        original_message_loop = hub._message_loop  # type: ignore[attr-defined]

        async def hooked_loop(session: WampSession) -> None:
            # Signal that we're in the loop
            check_event.set()
            # Wait for the test to check the session
            await pause_event.wait()
            # Then continue with GOODBYE
            goodbye: list[Any] = [
                WampMessageType.GOODBYE,
                {},
                "wamp.close.close_realm",
            ]
            ws.enqueue_text(json.dumps(goodbye))
            await original_message_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        # Start handle_websocket in background
        task = asyncio.create_task(hub.handle_websocket(ws))  # type: ignore[arg-type]

        # Wait for the session to enter the loop
        await check_event.wait()

        # Session should be tracked
        assert hub.session_count == 1
        sessions = list(hub.sessions.values())
        assert len(sessions) == 1
        assert sessions[0].is_open is True

        # Release the loop
        pause_event.set()
        await task

        # After disconnect, session is gone
        assert hub.session_count == 0
