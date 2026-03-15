"""WampHub: central orchestrator for WAMP sessions."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from fastapi_headless_wamp.errors import WampCanceled, WampError
from fastapi_headless_wamp.protocol import (
    WAMP_ERROR_CANCELED,
    WAMP_ERROR_NO_SUCH_PROCEDURE,
    WAMP_ERROR_NO_SUCH_SUBSCRIPTION,
    WAMP_ERROR_RUNTIME_ERROR,
    WampMessageType,
    validate_call,
    validate_cancel,
    validate_error,
    validate_register,
    validate_subscribe,
    validate_unregister,
    validate_unsubscribe,
    validate_yield,
)
from fastapi_headless_wamp.service import WampService
from fastapi_headless_wamp.session import (
    PROGRESSIVE_INPUT_END,
    ProgressiveCallInput,
    RpcHandler,
    WampSession,
    negotiate_subprotocol,
)

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
        # Counter for generating unique subscription IDs
        self._next_subscription_id: int = 0

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

        Supports three progressive features:

        1. **Progressive call results** (US-012): when the client sends
           ``receive_progress: true`` in the CALL options, the handler
           receives a ``_progress`` callback to send intermediate RESULTs.

        2. **Progressive call invocations** (US-014): when the client
           sends multiple CALL messages with the same ``request_id`` and
           ``options.progress = true``, the server accumulates chunks and
           passes an ``_input_chunks`` async iterator to the handler.  The
           final CALL (without the progress flag) signals end of input.
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

        is_progressive_input = bool(options.get("progress"))

        # ------------------------------------------------------------------
        # Progressive call invocations (client streams input to server)
        # ------------------------------------------------------------------

        # Case: subsequent progressive CALL (queue already exists)
        if session.has_progressive_input(request_id):
            queue = session.get_progressive_input_queue(request_id)
            if queue is not None:
                if is_progressive_input:
                    # Intermediate chunk
                    queue.put_nowait((call_args, call_kwargs))
                else:
                    # Final chunk: push data then signal end
                    queue.put_nowait((call_args, call_kwargs))
                    queue.put_nowait(PROGRESSIVE_INPUT_END)
            return

        # Case: first progressive CALL (start new handler)
        if is_progressive_input:
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

            queue, input_iter = session.create_progressive_input(request_id)
            # Push the first chunk
            queue.put_nowait((call_args, call_kwargs))

            # Launch handler in a task so we can continue processing messages
            task = asyncio.create_task(
                self._run_progressive_input_handler(
                    session, request_id, options, handler, input_iter
                )
            )
            session.store_progressive_input_task(request_id, task)
            # Also track as a running call task for CANCEL support
            session.store_running_call_task(request_id, task)

            def _on_prog_task_done(t: asyncio.Task[Any]) -> None:
                session.remove_running_call_task(request_id)

            task.add_done_callback(_on_prog_task_done)
            return

        # ------------------------------------------------------------------
        # Regular (non-progressive input) CALL handling
        # ------------------------------------------------------------------

        # Look up handler
        handler = self._server_rpcs.get(procedure)
        if handler is None:
            error_msg = [
                WampMessageType.ERROR,
                WampMessageType.CALL,
                request_id,
                {},
                WAMP_ERROR_NO_SUCH_PROCEDURE,
            ]
            await session.send_message(error_msg)
            return

        # Run handler in a task so the message loop can process CANCEL
        task = asyncio.create_task(
            self._invoke_rpc_handler(
                session, request_id, options, handler, call_args, call_kwargs, procedure
            )
        )
        session.store_running_call_task(request_id, task)

        # Add callback to clean up the task tracking when done
        def _on_task_done(t: asyncio.Task[Any]) -> None:
            session.remove_running_call_task(request_id)

        task.add_done_callback(_on_task_done)

    async def _run_progressive_input_handler(
        self,
        session: WampSession,
        request_id: int,
        options: dict[str, Any],
        handler: RpcHandler,
        input_iter: ProgressiveCallInput,
    ) -> None:
        """Run an RPC handler with progressive call input.

        The handler receives an ``_input_chunks`` parameter that is an
        async iterator yielding ``(args, kwargs)`` tuples for each
        progressive CALL from the client.

        The handler's final return value is sent as a RESULT.
        """
        try:
            # For progressive input handlers, only inject special kwargs
            # (not the CALL data kwargs — those come via the iterator)
            caller_kwargs: dict[str, Any] = {}
            if options.get("disclose_me"):
                caller_kwargs["_caller_session_id"] = session.session_id

            # Progressive call results: also support _progress for output
            receive_progress = bool(options.get("receive_progress"))
            sig = inspect.signature(handler)
            handler_accepts_progress = "_progress" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )

            if receive_progress and handler_accepts_progress:

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
                caller_kwargs["_progress"] = None

            # Inject _input_chunks if handler accepts it
            handler_accepts_input = "_input_chunks" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
            if handler_accepts_input:
                caller_kwargs["_input_chunks"] = input_iter

            # Determine timeout
            timeout_ms = options.get("timeout")
            timeout_s: float | None = None
            if (
                timeout_ms is not None
                and isinstance(timeout_ms, (int, float))
                and timeout_ms > 0
            ):
                timeout_s = timeout_ms / 1000.0

            if inspect.iscoroutinefunction(handler):
                coro = handler(**caller_kwargs)
            else:
                coro = asyncio.to_thread(handler, **caller_kwargs)

            if timeout_s is not None:
                result = await asyncio.wait_for(coro, timeout=timeout_s)
            else:
                result = await coro

        except asyncio.TimeoutError:
            error_msg: list[Any] = [
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
                    error_msg.append([])
                error_msg.append(exc.wamp_kwargs)
            await session.send_message(error_msg)
            return
        except Exception as exc:
            logger.error(
                "Session %d: progressive RPC handler raised: %s",
                session.session_id,
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
        finally:
            session.cleanup_progressive_input(request_id)

        # Build final RESULT
        result_msg: list[Any] = [
            WampMessageType.RESULT,
            request_id,
            {},
        ]
        if result is not None:
            result_msg.append([result])
        await session.send_message(result_msg)

    async def _invoke_rpc_handler(
        self,
        session: WampSession,
        request_id: int,
        options: dict[str, Any],
        handler: RpcHandler,
        call_args: list[Any],
        call_kwargs: dict[str, Any],
        procedure: str = "",
    ) -> None:
        """Invoke a regular (non-progressive-input) RPC handler.

        Handles caller_identification, progressive call results,
        timeout, and error handling.
        """
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
            # If CANCEL already sent the ERROR, suppress duplicate
            if session.is_request_cancelled(request_id):
                session.clear_cancelled_request(request_id)
                return
            error_msg: list[Any] = [
                WampMessageType.ERROR,
                WampMessageType.CALL,
                request_id,
                {},
                WAMP_ERROR_CANCELED,
            ]
            await session.send_message(error_msg)
            return
        except asyncio.CancelledError:
            # If CANCEL (skip/killnowait) already sent ERROR, suppress
            if session.is_request_cancelled(request_id):
                session.clear_cancelled_request(request_id)
                return
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
            # If CANCEL already sent the ERROR, suppress
            if session.is_request_cancelled(request_id):
                session.clear_cancelled_request(request_id)
                return
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
            # If CANCEL already sent the ERROR, suppress
            if session.is_request_cancelled(request_id):
                session.clear_cancelled_request(request_id)
                return
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

        # If CANCEL (skip mode) already sent ERROR, suppress RESULT
        if session.is_request_cancelled(request_id):
            session.clear_cancelled_request(request_id)
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
    # CANCEL handling (client cancels a pending CALL)
    # ------------------------------------------------------------------

    async def _handle_cancel(self, session: WampSession, msg: list[Any]) -> None:
        """Handle a CANCEL message from the client.

        CANCEL format: [CANCEL, CALL.Request|id, Options|dict]

        Supported ``options.mode`` values:

        * ``"skip"`` — Stop waiting for the result but let the handler
          continue running in the background.  Send ERROR
          ``wamp.error.canceled`` immediately.
        * ``"kill"`` — Cancel the running asyncio task and wait for
          cleanup to complete before sending ERROR ``wamp.error.canceled``.
        * ``"killnowait"`` — Cancel the running asyncio task but send
          ERROR ``wamp.error.canceled`` immediately without waiting for
          the task to finish.

        If no mode is specified the default behaviour is ``"kill"``.

        If the handler has already completed by the time CANCEL arrives,
        the CANCEL is silently ignored (the RESULT was already sent).
        """
        try:
            validate_cancel(msg)
        except WampError as exc:
            logger.warning(
                "Session %d: invalid CANCEL message: %s", session.session_id, exc
            )
            return

        request_id: int = msg[1]
        options: dict[str, Any] = msg[2]
        mode: str = options.get("mode", "kill")

        task = session.get_running_call_task(request_id)

        if task is None or task.done():
            # Handler already completed (RESULT was already sent) or
            # no matching task — silently ignore the CANCEL.
            logger.debug(
                "Session %d: CANCEL for request %d ignored (no running task)",
                session.session_id,
                request_id,
            )
            return

        if mode == "skip":
            # Stop waiting: send ERROR immediately, let handler continue
            session.mark_request_cancelled(request_id)
            error_msg: list[Any] = [
                WampMessageType.ERROR,
                WampMessageType.CALL,
                request_id,
                {},
                WAMP_ERROR_CANCELED,
            ]
            await session.send_message(error_msg)
            logger.info(
                "Session %d: CANCEL (skip) for request %d — "
                "sent canceled error, handler still running",
                session.session_id,
                request_id,
            )
        elif mode == "killnowait":
            # Cancel the task and send ERROR immediately without waiting
            session.mark_request_cancelled(request_id)
            task.cancel()
            error_msg = [
                WampMessageType.ERROR,
                WampMessageType.CALL,
                request_id,
                {},
                WAMP_ERROR_CANCELED,
            ]
            await session.send_message(error_msg)
            logger.info(
                "Session %d: CANCEL (killnowait) for request %d — "
                "task cancelled, sent canceled error",
                session.session_id,
                request_id,
            )
        else:
            # Default "kill" mode: cancel the task and wait for it to finish.
            # The task's CancelledError handler will send the ERROR message.
            task.cancel()
            logger.info(
                "Session %d: CANCEL (kill) for request %d — "
                "task cancelled, awaiting cleanup",
                session.session_id,
                request_id,
            )
            # Note: the _invoke_rpc_handler already handles CancelledError
            # and sends ERROR wamp.error.canceled, so we don't send it here.

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
    # Client SUBSCRIBE / UNSUBSCRIBE handling (PubSub)
    # ------------------------------------------------------------------

    def _generate_subscription_id(self) -> int:
        """Generate a unique subscription ID for a client subscription."""
        self._next_subscription_id += 1
        return self._next_subscription_id

    async def _handle_subscribe(self, session: WampSession, msg: list[Any]) -> None:
        """Handle a SUBSCRIBE message from the client.

        Stores the topic in the session's ``subscriptions`` map with a
        unique subscription ID and sends SUBSCRIBED back.

        Subscriptions are per-session (isolated, not shared across clients).
        """
        try:
            validate_subscribe(msg)
        except WampError as exc:
            logger.warning(
                "Session %d: invalid SUBSCRIBE message: %s", session.session_id, exc
            )
            return

        request_id: int = msg[1]
        # options: dict[str, Any] = msg[2]  # reserved for future use
        topic: str = msg[3]

        subscription_id = self._generate_subscription_id()

        # Store in session's subscription maps (per-session, isolated)
        session.subscriptions[subscription_id] = topic
        session.subscription_uris[topic] = subscription_id

        # Send SUBSCRIBED
        subscribed_msg: list[Any] = [
            WampMessageType.SUBSCRIBED,
            request_id,
            subscription_id,
        ]
        await session.send_message(subscribed_msg)
        logger.info(
            "Session %d: subscribed to '%s' (subscription_id=%d)",
            session.session_id,
            topic,
            subscription_id,
        )

    async def _handle_unsubscribe(self, session: WampSession, msg: list[Any]) -> None:
        """Handle an UNSUBSCRIBE message from the client.

        Removes the topic from the session's ``subscriptions`` map and
        sends UNSUBSCRIBED back.
        """
        try:
            validate_unsubscribe(msg)
        except WampError as exc:
            logger.warning(
                "Session %d: invalid UNSUBSCRIBE message: %s", session.session_id, exc
            )
            return

        request_id: int = msg[1]
        subscription_id: int = msg[2]

        # Look up subscription
        topic = session.subscriptions.get(subscription_id)
        if topic is None:
            error_msg: list[Any] = [
                WampMessageType.ERROR,
                WampMessageType.UNSUBSCRIBE,
                request_id,
                {},
                WAMP_ERROR_NO_SUCH_SUBSCRIPTION,
            ]
            await session.send_message(error_msg)
            return

        # Remove from session's maps
        del session.subscriptions[subscription_id]
        session.subscription_uris.pop(topic, None)

        # Send UNSUBSCRIBED
        unsubscribed_msg: list[Any] = [
            WampMessageType.UNSUBSCRIBED,
            request_id,
        ]
        await session.send_message(unsubscribed_msg)
        logger.info(
            "Session %d: unsubscribed from '%s' (subscription_id=%d)",
            session.session_id,
            topic,
            subscription_id,
        )

    # ------------------------------------------------------------------
    # YIELD handling (client responds to server's INVOCATION)
    # ------------------------------------------------------------------

    async def _handle_yield(self, session: WampSession, msg: list[Any]) -> None:
        """Handle a YIELD message from the client.

        Resolves the pending call Future for the corresponding INVOCATION,
        or invokes the progress callback for progressive YIELDs.

        Progressive YIELD: when ``options.progress`` is ``true``, the YIELD
        carries an intermediate result.  The hub looks up the progress
        callback stored by :meth:`WampSession.call` and invokes it.  The
        pending call future is **not** resolved until the final YIELD
        (without the progress flag) arrives.

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
        options: dict[str, Any] = msg[2] if len(msg) > 2 else {}
        yield_args: list[Any] = msg[3] if len(msg) > 3 else []
        # yield_kwargs: dict[str, Any] = msg[4] if len(msg) > 4 else {}

        # Extract result: single value or the args list
        if len(yield_args) == 1:
            result = yield_args[0]
        elif len(yield_args) > 1:
            result = yield_args
        else:
            result = None

        is_progressive = bool(options.get("progress"))

        if is_progressive:
            # Progressive YIELD: invoke on_progress callback if registered
            on_progress = session.get_progress_callback(request_id)
            if on_progress is not None:
                try:
                    await on_progress(result)
                except Exception as exc:
                    logger.error(
                        "Session %d: on_progress callback error for request %d: %s",
                        session.session_id,
                        request_id,
                        exc,
                        exc_info=True,
                    )
            else:
                # receive_progress=True but no callback: silently consume
                logger.debug(
                    "Session %d: progressive YIELD for request %d (no callback)",
                    session.session_id,
                    request_id,
                )
        else:
            # Final YIELD: resolve the pending call future
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

        # Use WampCanceled for the canceled error URI so callers can
        # distinguish cancellation from other errors.
        if error_uri == WAMP_ERROR_CANCELED:
            exception: WampError = WampCanceled(
                str(error_message), args=error_args, kwargs=error_kwargs
            )
        else:
            exception = WampError(
                str(error_message), args=error_args, kwargs=error_kwargs
            )
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
            # Wait for any running handler tasks to complete (or cancel)
            # before firing close callbacks and cleanup.  This ensures
            # fast handlers that are already in-flight get a chance to
            # send their RESULT/ERROR before the session is torn down.
            running_tasks = [
                t for t in session.get_all_running_call_tasks() if not t.done()
            ]
            if running_tasks:
                # Give running tasks a short grace period to finish
                _done, pending = await asyncio.wait(running_tasks, timeout=1.0)
                for t in pending:
                    t.cancel()
                # Wait for cancellation to propagate
                if pending:
                    await asyncio.wait(pending, timeout=1.0)

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
            elif msg_type == WampMessageType.CANCEL:
                await self._handle_cancel(session, msg)
            elif msg_type == WampMessageType.REGISTER:
                await self._handle_register(session, msg)
            elif msg_type == WampMessageType.UNREGISTER:
                await self._handle_unregister(session, msg)
            elif msg_type == WampMessageType.SUBSCRIBE:
                await self._handle_subscribe(session, msg)
            elif msg_type == WampMessageType.UNSUBSCRIBE:
                await self._handle_unsubscribe(session, msg)
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
