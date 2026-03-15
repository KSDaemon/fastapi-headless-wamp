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
    validate_error,
    validate_register,
    validate_unregister,
    validate_yield,
)
from fastapi_headless_wamp.service import WampService
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
        # Counter for generating unique registration IDs
        self._next_registration_id: int = 0

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
    # Service registration
    # ------------------------------------------------------------------

    def register_service(self, service: WampService) -> None:
        """Register all ``@rpc``-marked methods of a :class:`WampService`.

        Introspects the service instance for methods decorated with
        :func:`rpc`, constructs the full URI from the service prefix and
        the method's URI (or name), and registers them on the hub.

        The service's :attr:`~WampService.hub` attribute is set to this
        hub instance so that service methods can access it via
        ``self.hub``.

        Both ``async def`` and regular ``def`` methods are supported.
        Sync methods are automatically run via :func:`asyncio.to_thread`.
        """
        service.hub = self

        prefix = service.prefix
        for attr_name in dir(service):
            attr = getattr(service, attr_name)
            if not callable(attr):
                continue
            rpc_uri: str | None = getattr(attr, "_rpc_uri", None)
            # _rpc_uri is set by the @rpc() decorator.
            # It can be:
            #   - a string (explicit URI)
            #   - None (infer from method name)
            # If the attribute doesn't have _rpc_uri at all, skip it.
            if not hasattr(attr, "_rpc_uri"):
                continue

            # Build full URI
            if rpc_uri is not None:
                method_uri = rpc_uri
            else:
                method_uri = attr_name

            if prefix:
                full_uri = f"{prefix}.{method_uri}"
            else:
                full_uri = method_uri

            self._server_rpcs[full_uri] = attr
            logger.info(
                "Registered service RPC: %s (from %s.%s)",
                full_uri,
                type(service).__name__,
                attr_name,
            )

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

        Supports progressive call results: when the client sends
        ``receive_progress: true`` in the CALL options, the handler
        receives a ``_progress`` callback parameter that can be awaited
        to send intermediate RESULT messages with ``progress: true``
        in the details dict.  The handler's final return value is sent
        as the last RESULT (without the progress flag).

        When ``receive_progress`` is not set, ``_progress`` is ``None``.
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

        # Progressive call results support
        receive_progress = bool(options.get("receive_progress"))

        # Check if handler accepts _progress or **kwargs
        sig = inspect.signature(handler)
        handler_accepts_progress = "_progress" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )

        if receive_progress and handler_accepts_progress:
            # Build a _progress callback that sends progressive RESULTs
            async def _progress_callback(data: Any) -> None:
                progress_msg: list[Any] = [
                    WampMessageType.RESULT,
                    request_id,
                    {"progress": True},
                ]
                if data is not None:
                    progress_msg.append([data])
                await session.send_message(progress_msg)

            caller_kwargs["_progress"] = _progress_callback
        elif handler_accepts_progress:
            # Handler accepts _progress but client didn't request progressive
            caller_kwargs["_progress"] = None

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

        # Build final RESULT message (no progress flag)
        result_msg: list[Any] = [
            WampMessageType.RESULT,
            request_id,
            {},
        ]
        if result is not None:
            result_msg.append([result])
        await session.send_message(result_msg)

    # ------------------------------------------------------------------
    # Client REGISTER / UNREGISTER handling (client-side RPCs)
    # ------------------------------------------------------------------

    def _generate_registration_id(self) -> int:
        """Generate a unique registration ID for a client RPC registration."""
        self._next_registration_id += 1
        return self._next_registration_id

    async def _handle_register(self, session: WampSession, msg: list[Any]) -> None:
        """Handle a REGISTER message from the client.

        Stores the procedure URI in the session's ``client_rpcs`` map with
        a unique registration ID and sends REGISTERED back.

        Multiple clients can register the same URI independently
        (each in their own session, no conflict).
        """
        try:
            validate_register(msg)
        except WampError as exc:
            logger.warning(
                "Session %d: invalid REGISTER message: %s", session.session_id, exc
            )
            return

        request_id: int = msg[1]
        # options: dict[str, Any] = msg[2]  # reserved for future use
        procedure: str = msg[3]

        registration_id = self._generate_registration_id()

        # Store in session's client_rpcs maps (per-session, isolated)
        session.client_rpcs[registration_id] = procedure
        session.client_rpc_uris[procedure] = registration_id

        # Send REGISTERED
        registered_msg: list[Any] = [
            WampMessageType.REGISTERED,
            request_id,
            registration_id,
        ]
        await session.send_message(registered_msg)
        logger.info(
            "Session %d: registered client RPC '%s' (registration_id=%d)",
            session.session_id,
            procedure,
            registration_id,
        )

    async def _handle_unregister(self, session: WampSession, msg: list[Any]) -> None:
        """Handle an UNREGISTER message from the client.

        Removes the procedure from the session's ``client_rpcs`` map
        and sends UNREGISTERED back.
        """
        try:
            validate_unregister(msg)
        except WampError as exc:
            logger.warning(
                "Session %d: invalid UNREGISTER message: %s", session.session_id, exc
            )
            return

        request_id: int = msg[1]
        registration_id: int = msg[2]

        # Look up registration
        procedure = session.client_rpcs.get(registration_id)
        if procedure is None:
            error_msg: list[Any] = [
                WampMessageType.ERROR,
                WampMessageType.UNREGISTER,
                request_id,
                {},
                WAMP_ERROR_NO_SUCH_PROCEDURE,
            ]
            await session.send_message(error_msg)
            return

        # Remove from session's maps
        del session.client_rpcs[registration_id]
        session.client_rpc_uris.pop(procedure, None)

        # Send UNREGISTERED
        unregistered_msg: list[Any] = [
            WampMessageType.UNREGISTERED,
            request_id,
        ]
        await session.send_message(unregistered_msg)
        logger.info(
            "Session %d: unregistered client RPC '%s' (registration_id=%d)",
            session.session_id,
            procedure,
            registration_id,
        )

    # ------------------------------------------------------------------
    # YIELD handling (client responds to server's INVOCATION)
    # ------------------------------------------------------------------

    async def _handle_yield(self, session: WampSession, msg: list[Any]) -> None:
        """Handle a YIELD message from the client.

        Resolves the pending call Future for the corresponding INVOCATION.

        YIELD format: [YIELD, INVOCATION.Request|id, Options|dict, Arguments|list?, ArgumentsKw|dict?]
        """
        try:
            validate_yield(msg)
        except WampError as exc:
            logger.warning(
                "Session %d: invalid YIELD message: %s", session.session_id, exc
            )
            return

        request_id: int = msg[1]
        # options: dict[str, Any] = msg[2]  # reserved for future use (progressive)
        yield_args: list[Any] = msg[3] if len(msg) > 3 else []
        # yield_kwargs: dict[str, Any] = msg[4] if len(msg) > 4 else {}

        # Extract result: single value or the args list
        if len(yield_args) == 1:
            result = yield_args[0]
        elif len(yield_args) > 1:
            result = yield_args
        else:
            result = None

        session.resolve_pending_call(request_id, result)

    async def _handle_invocation_error(
        self, session: WampSession, msg: list[Any]
    ) -> None:
        """Handle an ERROR message in response to an INVOCATION.

        Rejects the pending call Future with an appropriate exception.

        ERROR format: [ERROR, REQUEST.Type|int, REQUEST.Request|id, Details|dict, Error|uri, Arguments|list?, ArgumentsKw|dict?]
        """
        try:
            validate_error(msg)
        except WampError as exc:
            logger.warning(
                "Session %d: invalid ERROR message: %s", session.session_id, exc
            )
            return

        request_id: int = msg[2]
        error_uri: str = msg[4]
        error_args: list[Any] = msg[5] if len(msg) > 5 else []
        error_kwargs: dict[str, Any] = msg[6] if len(msg) > 6 else {}

        error_message = error_args[0] if error_args else error_uri
        exception = WampError(str(error_message), args=error_args, kwargs=error_kwargs)
        # Set the URI from the error message
        exception.uri = error_uri

        session.reject_pending_call(request_id, exception)

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
            elif msg_type == WampMessageType.REGISTER:
                await self._handle_register(session, msg)
            elif msg_type == WampMessageType.UNREGISTER:
                await self._handle_unregister(session, msg)
            elif msg_type == WampMessageType.YIELD:
                await self._handle_yield(session, msg)
            elif msg_type == WampMessageType.ERROR:
                # ERROR can be in response to INVOCATION (server calling client)
                if len(msg) >= 3 and msg[1] == WampMessageType.INVOCATION:
                    await self._handle_invocation_error(session, msg)
                else:
                    logger.debug(
                        "Session %d: unhandled ERROR for request type %s",
                        session.session_id,
                        msg[1] if len(msg) > 1 else "unknown",
                    )
            else:
                # Future stories will add handlers for SUBSCRIBE, PUBLISH, etc.
                logger.debug(
                    "Session %d: unhandled message type %s",
                    session.session_id,
                    msg_type.name,
                )
