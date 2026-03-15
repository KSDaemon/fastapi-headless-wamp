"""WampHub: central orchestrator for WAMP sessions."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from fastapi_headless_wamp.errors import WampError
from fastapi_headless_wamp.protocol import (
    WAMP_ERROR_CANCELED,
    WAMP_ERROR_NO_SUCH_PROCEDURE,
    WAMP_ERROR_RUNTIME_ERROR,
    WampMessageType,
    validate_call,
)
from fastapi_headless_wamp.session import RpcHandler, WampSession, negotiate_subprotocol

logger = logging.getLogger(__name__)

# Type alias for lifecycle callbacks
SessionCallback = Callable[[WampSession], Awaitable[None]]


def _session_callback_list() -> list[SessionCallback]:
    return []


def _rpc_registry() -> dict[str, RpcHandler]:
    return {}


class WampHub:
    """Central hub that manages WAMP sessions and routes messages."""

    def __init__(self, realm: str = "realm1") -> None:
        self.realm = realm
        self._sessions: dict[int, WampSession] = {}
        self._server_rpcs: dict[str, RpcHandler] = _rpc_registry()
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
    # RPC registration decorator
    # ------------------------------------------------------------------

    def register(self, uri: str) -> Callable[..., Any]:
        """Decorator to register a function as a server-side RPC handler.

        Usage::

            @wamp.register("com.example.add")
            async def add(a: int, b: int) -> int:
                return a + b

        Both ``async def`` and regular ``def`` functions are supported.
        Sync functions are automatically run via :func:`asyncio.to_thread`.
        """

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self._server_rpcs[uri] = func
            logger.info("Registered server RPC: %s", uri)
            return func

        return decorator

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
    # CALL handling (client calls server-registered RPC)
    # ------------------------------------------------------------------

    async def _handle_call(self, session: WampSession, msg: list[Any]) -> None:
        """Handle a CALL message from the client.

        Looks up the procedure URI in the hub's server RPC registry,
        invokes the handler, and sends RESULT or ERROR back.
        """
        try:
            validate_call(msg)
        except WampError as exc:
            logger.warning(
                "Session %d: invalid CALL message: %s", session.session_id, exc
            )
            return

        request_id: int = msg[1]
        options: dict[str, Any] = msg[2]
        procedure: str = msg[3]
        call_args: list[Any] = msg[4] if len(msg) > 4 else []
        call_kwargs: dict[str, Any] = msg[5] if len(msg) > 5 else {}

        # Look up handler
        handler = self._server_rpcs.get(procedure)
        if handler is None:
            error_msg: list[Any] = [
                WampMessageType.ERROR,
                WampMessageType.CALL,
                request_id,
                {},
                WAMP_ERROR_NO_SUCH_PROCEDURE,
            ]
            await session.send_message(error_msg)
            return

        # caller_identification: pass caller session ID if disclose_me is set
        caller_kwargs = dict(call_kwargs)
        if options.get("disclose_me"):
            caller_kwargs["_caller_session_id"] = session.session_id

        # Determine timeout from options (milliseconds -> seconds)
        timeout_ms = options.get("timeout")
        timeout_s: float | None = None
        if (
            timeout_ms is not None
            and isinstance(timeout_ms, (int, float))
            and timeout_ms > 0
        ):
            timeout_s = timeout_ms / 1000.0

        # Invoke handler
        try:
            if inspect.iscoroutinefunction(handler):
                coro = handler(*call_args, **caller_kwargs)
            else:
                # Sync handler: run in thread
                coro = asyncio.to_thread(handler, *call_args, **caller_kwargs)

            if timeout_s is not None:
                result = await asyncio.wait_for(coro, timeout=timeout_s)
            else:
                result = await coro

        except asyncio.TimeoutError:
            error_msg = [
                WampMessageType.ERROR,
                WampMessageType.CALL,
                request_id,
                {},
                WAMP_ERROR_CANCELED,
            ]
            await session.send_message(error_msg)
            return
        except asyncio.CancelledError:
            error_msg = [
                WampMessageType.ERROR,
                WampMessageType.CALL,
                request_id,
                {},
                WAMP_ERROR_CANCELED,
            ]
            await session.send_message(error_msg)
            return
        except WampError as exc:
            # WAMP-typed exception from handler
            error_msg = [
                WampMessageType.ERROR,
                WampMessageType.CALL,
                request_id,
                {},
                exc.uri,
            ]
            if exc.wamp_args:
                error_msg.append(exc.wamp_args)
            if exc.wamp_kwargs:
                if len(error_msg) == 5:
                    error_msg.append([])  # args placeholder
                error_msg.append(exc.wamp_kwargs)
            await session.send_message(error_msg)
            return
        except Exception as exc:
            logger.error(
                "Session %d: RPC handler '%s' raised: %s",
                session.session_id,
                procedure,
                exc,
                exc_info=True,
            )
            error_msg = [
                WampMessageType.ERROR,
                WampMessageType.CALL,
                request_id,
                {},
                WAMP_ERROR_RUNTIME_ERROR,
                [str(exc)],
            ]
            await session.send_message(error_msg)
            return

        # Build RESULT message
        result_msg: list[Any] = [
            WampMessageType.RESULT,
            request_id,
            {},
        ]
        if result is not None:
            result_msg.append([result])
        await session.send_message(result_msg)

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
            elif msg_type == WampMessageType.CALL:
                await self._handle_call(session, msg)
            else:
                # Future stories will add handlers for REGISTER, SUBSCRIBE, etc.
                logger.debug(
                    "Session %d: unhandled message type %s",
                    session.session_id,
                    msg_type.name,
                )
