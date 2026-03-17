"""Tests for client REGISTER/UNREGISTER handling (US-010).

Covers:
- Client sends REGISTER -> server stores in session's client_rpcs, sends REGISTERED
- Client sends UNREGISTER -> server removes from session's client_rpcs, sends UNREGISTERED
- UNREGISTER for non-existent registration -> ERROR wamp.error.no_such_procedure
- Multiple clients can register the same URI independently (each in their own session)
- All client registrations cleaned up on session disconnect
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
    WAMP_ERROR_NO_SUCH_PROCEDURE,
    WampMessageType,
)
from fastapi_headless_wamp.session import WampSession

# ---------------------------------------------------------------------------
# Mock WebSocket helper (same as test_hub.py)
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


def make_unregister(request_id: int, registration_id: int) -> list[Any]:
    return [WampMessageType.UNREGISTER, request_id, registration_id]


# ---------------------------------------------------------------------------
# REGISTER tests
# ---------------------------------------------------------------------------


class TestRegister:
    """Client sends REGISTER -> server responds with REGISTERED."""

    async def test_register_success(self) -> None:
        """REGISTER stores procedure in session's client_rpcs and sends REGISTERED."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.add")))
        ws.enqueue_text(json.dumps(make_goodbye()))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # Messages: WELCOME, REGISTERED, GOODBYE reply
        assert len(ws.sent_texts) == 3

        registered = parse_sent(ws, 1)
        assert registered[0] == WampMessageType.REGISTERED
        assert registered[1] == 1  # matches request_id
        assert isinstance(registered[2], int)  # registration_id
        assert registered[2] > 0

    async def test_register_multiple_procedures(self) -> None:
        """Multiple REGISTER messages create independent registrations."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.add")))
        ws.enqueue_text(json.dumps(make_register(2, "com.example.subtract")))
        ws.enqueue_text(json.dumps(make_goodbye()))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # WELCOME + 2x REGISTERED + GOODBYE reply
        assert len(ws.sent_texts) == 4

        reg1 = parse_sent(ws, 1)
        reg2 = parse_sent(ws, 2)
        assert reg1[0] == WampMessageType.REGISTERED
        assert reg2[0] == WampMessageType.REGISTERED
        # Different registration IDs
        assert reg1[2] != reg2[2]

    async def test_register_stores_in_session_maps(self) -> None:
        """REGISTER populates both client_rpcs and client_rpc_uris maps."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.ping")))

        # Use event to inspect session state mid-connection
        entered_loop = asyncio.Event()
        release_loop = asyncio.Event()

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            # Process the REGISTER message
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            entered_loop.set()
            await release_loop.wait()

            # Enqueue GOODBYE and process it
            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        task = asyncio.create_task(hub.handle_websocket(ws))  # type: ignore[arg-type]
        await entered_loop.wait()

        session = list(hub.sessions.values())[0]

        # Verify maps are populated
        assert len(session.client_rpcs) == 1
        assert len(session.client_rpc_uris) == 1
        reg_id = list(session.client_rpcs.keys())[0]
        assert session.client_rpcs[reg_id] == "com.example.ping"
        assert session.client_rpc_uris["com.example.ping"] == reg_id

        release_loop.set()
        await task

    async def test_register_unique_registration_ids(self) -> None:
        """Each REGISTER gets a unique, incrementing registration ID."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(10, "com.a")))
        ws.enqueue_text(json.dumps(make_register(20, "com.b")))
        ws.enqueue_text(json.dumps(make_register(30, "com.c")))
        ws.enqueue_text(json.dumps(make_goodbye()))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        reg_ids: list[int] = []
        for i in range(1, 4):  # indices 1, 2, 3 (after WELCOME at 0)
            msg = parse_sent(ws, i)
            assert msg[0] == WampMessageType.REGISTERED
            reg_ids.append(msg[2])

        # All unique
        assert len(set(reg_ids)) == 3
        # Monotonically increasing
        assert reg_ids == sorted(reg_ids)

    async def test_register_request_id_echo(self) -> None:
        """REGISTERED echoes the correct request_id from REGISTER."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(42, "com.example.foo")))
        ws.enqueue_text(json.dumps(make_goodbye()))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        registered = parse_sent(ws, 1)
        assert registered[0] == WampMessageType.REGISTERED
        assert registered[1] == 42  # request_id echoed


# ---------------------------------------------------------------------------
# UNREGISTER tests
# ---------------------------------------------------------------------------


class TestUnregister:
    """Client sends UNREGISTER -> server responds with UNREGISTERED."""

    async def test_unregister_success(self) -> None:
        """UNREGISTER removes procedure from session and sends UNREGISTERED."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.add")))
        # We need the registration_id from the REGISTERED reply
        # Use hooked loop approach to get it

        entered_loop = asyncio.Event()
        release_loop = asyncio.Event()

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            # Process REGISTER
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            # Get registration_id from the REGISTERED message
            registered = parse_sent(ws, 1)
            reg_id = registered[2]

            # Enqueue UNREGISTER with the correct registration_id
            ws.enqueue_text(json.dumps(make_unregister(2, reg_id)))
            ws.enqueue_text(json.dumps(make_goodbye()))

            entered_loop.set()
            await release_loop.wait()
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        task = asyncio.create_task(hub.handle_websocket(ws))  # type: ignore[arg-type]
        await entered_loop.wait()
        release_loop.set()
        await task

        # WELCOME, REGISTERED, UNREGISTERED, GOODBYE reply
        assert len(ws.sent_texts) == 4

        unregistered = parse_sent(ws, 2)
        assert unregistered[0] == WampMessageType.UNREGISTERED
        assert unregistered[1] == 2  # request_id echoed

    async def test_unregister_removes_from_session_maps(self) -> None:
        """UNREGISTER clears both client_rpcs and client_rpc_uris."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.ping")))

        check_done = asyncio.Event()
        release = asyncio.Event()

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            # Process REGISTER
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            # Verify registered
            assert len(session.client_rpcs) == 1
            registered = parse_sent(ws, 1)
            reg_id = registered[2]

            # Process UNREGISTER
            ws.enqueue_text(json.dumps(make_unregister(2, reg_id)))
            msg2 = await session.receive_message()
            await hub._handle_unregister(session, msg2)

            # Verify unregistered
            assert len(session.client_rpcs) == 0
            assert len(session.client_rpc_uris) == 0

            check_done.set()
            await release.wait()

            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        task = asyncio.create_task(hub.handle_websocket(ws))  # type: ignore[arg-type]
        await check_done.wait()
        release.set()
        await task

    async def test_unregister_nonexistent_returns_error(self) -> None:
        """UNREGISTER for non-existent registration_id sends ERROR."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        # UNREGISTER with a registration_id that doesn't exist
        ws.enqueue_text(json.dumps(make_unregister(1, 99999)))
        ws.enqueue_text(json.dumps(make_goodbye()))

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # WELCOME, ERROR, GOODBYE reply
        assert len(ws.sent_texts) == 3

        error = parse_sent(ws, 1)
        assert error[0] == WampMessageType.ERROR
        assert error[1] == WampMessageType.UNREGISTER
        assert error[2] == 1  # request_id echoed
        assert error[4] == WAMP_ERROR_NO_SUCH_PROCEDURE

    async def test_unregister_already_unregistered_returns_error(self) -> None:
        """UNREGISTER the same registration_id twice sends ERROR on second attempt."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.add")))

        done = asyncio.Event()
        release = asyncio.Event()

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            # Process REGISTER
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            registered = parse_sent(ws, 1)
            reg_id = registered[2]

            # First UNREGISTER (success)
            ws.enqueue_text(json.dumps(make_unregister(2, reg_id)))
            # Second UNREGISTER (error)
            ws.enqueue_text(json.dumps(make_unregister(3, reg_id)))
            ws.enqueue_text(json.dumps(make_goodbye()))

            done.set()
            await release.wait()
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        task = asyncio.create_task(hub.handle_websocket(ws))  # type: ignore[arg-type]
        await done.wait()
        release.set()
        await task

        # WELCOME, REGISTERED, UNREGISTERED, ERROR, GOODBYE reply
        assert len(ws.sent_texts) == 5

        unregistered = parse_sent(ws, 2)
        assert unregistered[0] == WampMessageType.UNREGISTERED
        assert unregistered[1] == 2

        error = parse_sent(ws, 3)
        assert error[0] == WampMessageType.ERROR
        assert error[1] == WampMessageType.UNREGISTER
        assert error[2] == 3
        assert error[4] == WAMP_ERROR_NO_SUCH_PROCEDURE


# ---------------------------------------------------------------------------
# Independent registrations across sessions
# ---------------------------------------------------------------------------


class TestIndependentRegistrations:
    """Multiple clients can register the same URI independently."""

    async def test_same_uri_different_sessions(self) -> None:
        """Two sessions can both register 'com.example.add' without conflict."""
        hub = WampHub(realm="realm1")

        ws1 = MockWebSocket(subprotocols=["wamp.2.json"])
        ws2 = MockWebSocket(subprotocols=["wamp.2.json"])

        # Use events to coordinate two concurrent sessions
        both_registered = asyncio.Event()
        release = asyncio.Event()

        sessions_seen: list[WampSession] = []

        original_loop = hub._message_loop

        call_count = 0

        async def hooked_loop(session: WampSession) -> None:
            nonlocal call_count
            call_count += 1
            my_count = call_count

            sessions_seen.append(session)

            # Process REGISTER
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            if my_count == 2:
                both_registered.set()

            await release.wait()

            ws = ws1 if my_count == 1 else ws2
            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        # Enqueue HELLO + REGISTER for both
        ws1.enqueue_text(json.dumps(make_hello()))
        ws1.enqueue_text(json.dumps(make_register(1, "com.example.add")))

        ws2.enqueue_text(json.dumps(make_hello()))
        ws2.enqueue_text(json.dumps(make_register(1, "com.example.add")))

        task1 = asyncio.create_task(hub.handle_websocket(ws1))  # type: ignore[arg-type]
        task2 = asyncio.create_task(hub.handle_websocket(ws2))  # type: ignore[arg-type]

        await both_registered.wait()

        # Both sessions should have the procedure registered
        assert len(sessions_seen) == 2
        s1, s2 = sessions_seen
        assert "com.example.add" in s1.client_rpc_uris
        assert "com.example.add" in s2.client_rpc_uris

        # Registration IDs should be different (global counter)
        reg_id_1 = s1.client_rpc_uris["com.example.add"]
        reg_id_2 = s2.client_rpc_uris["com.example.add"]
        assert reg_id_1 != reg_id_2

        release.set()
        await task1
        await task2

    async def test_unregister_in_one_session_does_not_affect_other(self) -> None:
        """Unregistering in one session doesn't affect another session's registration."""
        hub = WampHub(realm="realm1")

        ws1 = MockWebSocket(subprotocols=["wamp.2.json"])
        ws2 = MockWebSocket(subprotocols=["wamp.2.json"])

        both_registered = asyncio.Event()
        s1_unregistered = asyncio.Event()
        release = asyncio.Event()

        sessions_seen: list[WampSession] = []

        original_loop = hub._message_loop

        call_count = 0

        async def hooked_loop(session: WampSession) -> None:
            nonlocal call_count
            call_count += 1
            my_count = call_count

            sessions_seen.append(session)

            # Process REGISTER
            msg = await session.receive_message()
            await hub._handle_register(session, msg)

            if my_count == 2:
                both_registered.set()

            if my_count == 1:
                # Wait for both to be registered, then unregister
                await both_registered.wait()
                reg_id = session.client_rpc_uris["com.example.add"]
                ws1.enqueue_text(json.dumps(make_unregister(2, reg_id)))
                unreg_msg = await session.receive_message()
                await hub._handle_unregister(session, unreg_msg)
                s1_unregistered.set()

            if my_count == 2:
                await s1_unregistered.wait()

            await release.wait()

            ws = ws1 if my_count == 1 else ws2
            ws.enqueue_text(json.dumps(make_goodbye()))
            await original_loop(session)

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        ws1.enqueue_text(json.dumps(make_hello()))
        ws1.enqueue_text(json.dumps(make_register(1, "com.example.add")))

        ws2.enqueue_text(json.dumps(make_hello()))
        ws2.enqueue_text(json.dumps(make_register(1, "com.example.add")))

        task1 = asyncio.create_task(hub.handle_websocket(ws1))  # type: ignore[arg-type]
        task2 = asyncio.create_task(hub.handle_websocket(ws2))  # type: ignore[arg-type]

        await s1_unregistered.wait()

        s1, s2 = sessions_seen
        # Session 1 has unregistered
        assert len(s1.client_rpcs) == 0
        assert "com.example.add" not in s1.client_rpc_uris
        # Session 2 still has it
        assert "com.example.add" in s2.client_rpc_uris
        assert len(s2.client_rpcs) == 1

        release.set()
        await task1
        await task2


# ---------------------------------------------------------------------------
# Cleanup on disconnect
# ---------------------------------------------------------------------------


class TestCleanupOnDisconnect:
    """All client registrations cleaned up on session disconnect."""

    async def test_registrations_cleared_on_disconnect(self) -> None:
        """When session disconnects, client_rpcs and client_rpc_uris are cleared."""
        from starlette.websockets import WebSocketDisconnect

        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.add")))
        ws.enqueue_text(json.dumps(make_register(2, "com.example.sub")))

        registered_event = asyncio.Event()

        session_ref: list[WampSession] = []

        original_loop = hub._message_loop

        async def hooked_loop(session: WampSession) -> None:
            # Process two REGISTERs
            msg1 = await session.receive_message()
            await hub._handle_register(session, msg1)
            msg2 = await session.receive_message()
            await hub._handle_register(session, msg2)

            session_ref.append(session)
            registered_event.set()

            # Simulate disconnect
            raise WebSocketDisconnect()

        hub._message_loop = hooked_loop  # type: ignore[method-assign]

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # After disconnect, session cleanup should have cleared everything
        session = session_ref[0]
        assert len(session.client_rpcs) == 0
        assert len(session.client_rpc_uris) == 0
        assert session.is_open is False

    async def test_registrations_cleared_on_goodbye(self) -> None:
        """When session sends GOODBYE, registrations are cleaned up."""
        hub = WampHub(realm="realm1")
        ws = MockWebSocket(subprotocols=["wamp.2.json"])

        ws.enqueue_text(json.dumps(make_hello()))
        ws.enqueue_text(json.dumps(make_register(1, "com.example.add")))
        ws.enqueue_text(json.dumps(make_goodbye()))

        session_ref: list[WampSession] = []

        @hub.on_session_close
        async def on_close(session: WampSession) -> None:
            # At this point session is still in hub but cleanup hasn't run yet
            # so we should see the registration still present
            session_ref.append(session)

        await hub.handle_websocket(ws)  # type: ignore[arg-type]

        # After handle_websocket returns, cleanup has run
        session = session_ref[0]
        assert len(session.client_rpcs) == 0
        assert len(session.client_rpc_uris) == 0


# ---------------------------------------------------------------------------
# FastAPI integration tests
# ---------------------------------------------------------------------------


class TestFastAPIIntegrationRegister:
    """Integration tests using FastAPI TestClient for REGISTER/UNREGISTER."""

    def _make_app(self, hub: WampHub) -> FastAPI:
        app = FastAPI()

        @app.websocket("/ws")
        async def wamp_endpoint(websocket: FastAPIWebSocket) -> None:
            await hub.handle_websocket(websocket)

        return app

    def test_register_via_fastapi(self) -> None:
        """Full register flow via FastAPI test client."""
        hub = WampHub(realm="realm1")
        app = self._make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                ws.send_json(make_hello())
                welcome: list[Any] = ws.receive_json()
                assert welcome[0] == WampMessageType.WELCOME

                # Register a procedure
                ws.send_json(make_register(1, "com.example.echo"))
                registered: list[Any] = ws.receive_json()
                assert registered[0] == WampMessageType.REGISTERED
                assert registered[1] == 1
                reg_id = registered[2]
                assert isinstance(reg_id, int)
                assert reg_id > 0

                # Unregister it
                ws.send_json(make_unregister(2, reg_id))
                unregistered: list[Any] = ws.receive_json()
                assert unregistered[0] == WampMessageType.UNREGISTERED
                assert unregistered[1] == 2

                # Goodbye
                ws.send_json(make_goodbye())
                goodbye_reply: list[Any] = ws.receive_json()
                assert goodbye_reply[0] == WampMessageType.GOODBYE

        assert hub.session_count == 0

    def test_unregister_nonexistent_via_fastapi(self) -> None:
        """UNREGISTER with invalid registration_id returns ERROR via FastAPI."""
        hub = WampHub(realm="realm1")
        app = self._make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                ws.send_json(make_hello())
                ws.receive_json()  # WELCOME

                ws.send_json(make_unregister(1, 12345))
                error: list[Any] = ws.receive_json()
                assert error[0] == WampMessageType.ERROR
                assert error[1] == WampMessageType.UNREGISTER
                assert error[2] == 1
                assert error[4] == WAMP_ERROR_NO_SUCH_PROCEDURE

                ws.send_json(make_goodbye())
                ws.receive_json()  # GOODBYE reply

        assert hub.session_count == 0

    def test_register_multiple_and_unregister_one_via_fastapi(self) -> None:
        """Register multiple procedures, unregister one, verify other remains."""
        hub = WampHub(realm="realm1")
        app = self._make_app(hub)

        with TestClient(app) as client:
            with client.websocket_connect("/ws", subprotocols=["wamp.2.json"]) as ws:
                ws.send_json(make_hello())
                ws.receive_json()  # WELCOME

                # Register two procedures
                ws.send_json(make_register(1, "com.example.add"))
                reg1: list[Any] = ws.receive_json()
                assert reg1[0] == WampMessageType.REGISTERED
                reg_id_1 = reg1[2]

                ws.send_json(make_register(2, "com.example.sub"))
                reg2: list[Any] = ws.receive_json()
                assert reg2[0] == WampMessageType.REGISTERED
                reg_id_2 = reg2[2]

                assert reg_id_1 != reg_id_2

                # Unregister only the first
                ws.send_json(make_unregister(3, reg_id_1))
                unreg: list[Any] = ws.receive_json()
                assert unreg[0] == WampMessageType.UNREGISTERED
                assert unreg[1] == 3

                # Unregistering it again should fail
                ws.send_json(make_unregister(4, reg_id_1))
                error: list[Any] = ws.receive_json()
                assert error[0] == WampMessageType.ERROR
                assert error[4] == WAMP_ERROR_NO_SUCH_PROCEDURE

                # Second registration should still work to unregister
                ws.send_json(make_unregister(5, reg_id_2))
                unreg2: list[Any] = ws.receive_json()
                assert unreg2[0] == WampMessageType.UNREGISTERED
                assert unreg2[1] == 5

                ws.send_json(make_goodbye())
                ws.receive_json()  # GOODBYE reply

        assert hub.session_count == 0
