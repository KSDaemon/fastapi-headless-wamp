"""WampHub: central orchestrator for WAMP sessions."""

from __future__ import annotations

import logging
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from fastapi_headless_wamp.protocol import WampMessageType
from fastapi_headless_wamp.session import WampSession, negotiate_subprotocol

logger = logging.getLogger(__name__)


class WampHub:
    """Central hub that manages WAMP sessions and routes messages."""

    def __init__(self, realm: str = "realm1") -> None:
        self.realm = realm
        self._sessions: dict[int, WampSession] = {}

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

        try:
            # Message dispatch loop
            await self._message_loop(session)
        except WebSocketDisconnect:
            logger.info("Hub: session %d disconnected", session.session_id)
        except Exception as exc:
            logger.error("Hub: session %d error: %s", session.session_id, exc)
        finally:
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
