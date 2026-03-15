"""WampHub: central orchestrator for WAMP sessions."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from fastapi_headless_wamp.protocol import WampMessageType
from fastapi_headless_wamp.session import WampSession, negotiate_subprotocol

logger = logging.getLogger(__name__)

# Type alias for lifecycle callbacks
SessionCallback = Callable[[WampSession], Awaitable[None]]


def _session_callback_list() -> list[SessionCallback]:
    return []


class WampHub:
    """Central hub that manages WAMP sessions and routes messages."""

    def __init__(self, realm: str = "realm1") -> None:
        self.realm = realm
        self._sessions: dict[int, WampSession] = {}
        self._on_session_open_callbacks: list[SessionCallback] = (
            _session_callback_list()
        )
        self._on_session_close_callbacks: list[SessionCallback] = (
            _session_callback_list()
        )

    @property
    def sessions(self) -> dict[int, WampSession]:
        """Active sessions: session_id -> WampSession."""
        return self._sessions

    @property
    def session_count(self) -> int:
        """Number of active sessions."""
        return len(self._sessions)

    def _active_session_ids(self) -> set[int]:
        """Return the set of currently active session IDs."""
        return set(self._sessions.keys())

    # ------------------------------------------------------------------
    # Lifecycle callback decorators
    # ------------------------------------------------------------------

    def on_session_open(self, func: SessionCallback) -> SessionCallback:
        """Decorator to register a callback invoked when a session opens.

        The callback receives the :class:`WampSession` that just completed
        the WAMP handshake.  It must be an ``async def`` function.

        Usage::

            @wamp.on_session_open
            async def greet(session: WampSession) -> None:
                print(f"Session {session.session_id} opened")
        """
        self._on_session_open_callbacks.append(func)
        return func

    def on_session_close(self, func: SessionCallback) -> SessionCallback:
        """Decorator to register a callback invoked when a session closes.

        The callback receives the :class:`WampSession` that is about to
        be cleaned up.  It must be an ``async def`` function.

        Usage::

            @wamp.on_session_close
            async def farewell(session: WampSession) -> None:
                print(f"Session {session.session_id} closed")
        """
        self._on_session_close_callbacks.append(func)
        return func

    async def _fire_session_open(self, session: WampSession) -> None:
        """Invoke all on_session_open callbacks."""
        for cb in self._on_session_open_callbacks:
            try:
                await cb(session)
            except Exception as exc:
                logger.error("on_session_open callback error: %s", exc, exc_info=True)

    async def _fire_session_close(self, session: WampSession) -> None:
        """Invoke all on_session_close callbacks."""
        for cb in self._on_session_close_callbacks:
            try:
                await cb(session)
            except Exception as exc:
                logger.error("on_session_close callback error: %s", exc, exc_info=True)

    # ------------------------------------------------------------------
    # WebSocket handler
    # ------------------------------------------------------------------

    async def handle_websocket(self, websocket: WebSocket) -> None:
        """Full WAMP WebSocket handler.

        Negotiates subprotocol, performs handshake, enters the message
        dispatch loop, and cleans up on disconnect.
        """
        # Negotiate subprotocol
        requested: list[str] = websocket.scope.get("subprotocols", [])  # type: ignore[assignment]
        result = negotiate_subprotocol(requested)
        if result is None:
            logger.warning("No matching WAMP subprotocol from client: %s", requested)
            await websocket.close(code=1002)
            return

        subprotocol, serializer = result
        await websocket.accept(subprotocol=subprotocol)

        session = WampSession(websocket, serializer)

        # Handshake
        success = await session.perform_handshake(
            self.realm,
            existing_ids=self._active_session_ids(),
        )
        if not success:
            try:
                await websocket.close()
            except Exception:
                pass
            return

        # Register session
        self._sessions[session.session_id] = session
        logger.info(
            "Hub: session %d connected (realm=%s, sessions=%d)",
            session.session_id,
            session.realm,
            self.session_count,
        )

        # Fire on_session_open callbacks
        await self._fire_session_open(session)

        try:
            # Message dispatch loop
            await self._message_loop(session)
        except WebSocketDisconnect:
            logger.info("Hub: session %d disconnected", session.session_id)
        except Exception as exc:
            logger.error("Hub: session %d error: %s", session.session_id, exc)
        finally:
            # Fire on_session_close callbacks before cleanup
            await self._fire_session_close(session)

            # Cleanup
            session.cleanup()
            self._sessions.pop(session.session_id, None)
            logger.info(
                "Hub: session %d removed (sessions=%d)",
                session.session_id,
                self.session_count,
            )

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    async def _message_loop(self, session: WampSession) -> None:
        """Read messages from the session and dispatch by type."""
        async for msg in session:
            if not msg:
                continue
            msg_type_code: Any = msg[0]
            try:
                msg_type = WampMessageType(msg_type_code)
            except (ValueError, TypeError):
                logger.warning(
                    "Session %d: unknown message type %s",
                    session.session_id,
                    msg_type_code,
                )
                continue

            if msg_type == WampMessageType.GOODBYE:
                await session.handle_goodbye(msg)
                break
            else:
                # Future stories will add handlers for CALL, REGISTER, etc.
                logger.debug(
                    "Session %d: unhandled message type %s",
                    session.session_id,
                    msg_type.name,
                )
