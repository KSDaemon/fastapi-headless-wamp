"""WAMP session wrapping a FastAPI WebSocket."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from fastapi_headless_wamp.errors import (
    WampCallTimeoutError,
    WampError,
    WampNoSuchProcedureError,
)

# Sentinel value to signal end of progressive input stream
PROGRESSIVE_INPUT_END = object()
from fastapi_headless_wamp.protocol import (
    WAMP_ERROR_CLOSE_REALM,
    WAMP_ERROR_GOODBYE_AND_OUT,
    WampMessageType,
    validate_hello,
)
from fastapi_headless_wamp.serializers import (
    WAMP_SUBPROTOCOL_PREFIX,
    Serializer,
    get_available_subprotocols,
    get_serializer,
)

logger = logging.getLogger(__name__)

# Type aliases for internal maps
RpcHandler = Callable[..., Any]
ProgressCallback = Callable[[Any], Awaitable[None]]

# XXX: Mark as const?
# WAMP role features advertised by the server
DEALER_FEATURES: dict[str, bool] = {
    "progressive_call_results": True,
    "call_canceling": True,
    "caller_identification": True,
    "call_timeout": True,
}

# XXX: Mark as const?
BROKER_FEATURES: dict[str, bool] = {
    "publisher_identification": True,
    "publisher_exclusion": True,
    "subscriber_blackwhite_listing": True,
}


# Typed default factories for pyright strict mode
def _str_handler_dict() -> dict[str, RpcHandler]:
    return {}


def _int_str_dict() -> dict[int, str]:
    return {}


def _str_int_dict() -> dict[str, int]:
    return {}


def _int_future_dict() -> dict[int, asyncio.Future[Any]]:
    return {}


def _int_sub_dict() -> dict[int, str]:
    return {}


def _int_progress_dict() -> dict[int, ProgressCallback]:
    return {}


def _int_queue_dict() -> dict[int, asyncio.Queue[Any]]:
    return {}


def _int_task_dict() -> dict[int, asyncio.Task[Any]]:
    return {}


class ProgressiveCallInput:
    """Async iterator over progressive CALL input chunks.

    RPC handlers receive an instance of this class as the
    ``_input_chunks`` parameter when a client sends progressive CALLs
    (multiple CALL messages with the same ``request_id`` and
    ``options.progress = true``).

    Usage in a handler::

        async def my_handler(_input_chunks: ProgressiveCallInput | None = None) -> str:
            if _input_chunks is not None:
                async for chunk_args, chunk_kwargs in _input_chunks:
                    process(chunk_args, chunk_kwargs)
            return "done"

    Each iteration yields a tuple ``(args: list[Any], kwargs: dict[str, Any])``
    from the progressive CALL message.
    """

    def __init__(self, queue: asyncio.Queue[Any]) -> None:
        self._queue = queue

    def __aiter__(self) -> ProgressiveCallInput:
        return self

    async def __anext__(self) -> tuple[list[Any], dict[str, Any]]:
        item = await self._queue.get()
        if item is PROGRESSIVE_INPUT_END:
            raise StopAsyncIteration
        args: list[Any] = item[0]
        kwargs: dict[str, Any] = item[1]
        return args, kwargs


# XXX: Move to protocol?
def negotiate_subprotocol(
    requested_subprotocols: list[str],
) -> tuple[str, Serializer] | None:
    """Match a WAMP subprotocol from the client's list.

    Returns ``(subprotocol_string, serializer)`` for the first match,
    or ``None`` if no match is found.
    """
    available = get_available_subprotocols()
    for subproto in requested_subprotocols:
        if subproto in available:
            # Extract protocol name from "wamp.2.<protocol>"
            protocol_name = subproto[len(WAMP_SUBPROTOCOL_PREFIX) :]
            serializer = get_serializer(protocol_name)
            return subproto, serializer
    return None


class WampSession:
    """Represents a single WAMP session over a WebSocket connection.

    Wraps a Starlette/FastAPI WebSocket and a Serializer to provide
    typed WAMP message send/receive, async iteration, and per-session
    state management for RPCs and subscriptions.
    """

    def __init__(self, websocket: WebSocket, serializer: Serializer) -> None:
        self._websocket = websocket
        self._serializer = serializer

        # Session identity (set during handshake)
        self.session_id: int = 0
        self.realm: str = ""
        self.is_open: bool = False

        # --- Per-session state containers ---

        # Server-side RPCs registered on this session's hub (uri -> handler)
        # (populated by WampHub, not managed here)
        self.server_rpcs: dict[str, RpcHandler] = _str_handler_dict()

        # Client-registered RPCs: registration_id -> procedure URI
        self.client_rpcs: dict[int, str] = _int_str_dict()
        # Reverse lookup: procedure URI -> registration_id
        self.client_rpc_uris: dict[str, int] = _str_int_dict()

        # Client subscriptions: subscription_id -> topic URI
        self.subscriptions: dict[int, str] = _int_sub_dict()
        # Reverse lookup: topic URI -> subscription_id
        self.subscription_uris: dict[str, int] = _str_int_dict()

        # Server-side subscription handlers (topic -> handler)
        self.server_subscriptions: dict[str, RpcHandler] = _str_handler_dict()

        # Pending calls TO client: request_id -> Future
        self.pending_calls: dict[int, asyncio.Future[Any]] = _int_future_dict()

        # Progress callbacks for pending calls: request_id -> on_progress callback
        self._pending_progress_callbacks: dict[int, ProgressCallback] = _int_progress_dict()

        # Progressive call input state: request_id -> queue for incoming chunks
        self._progressive_input_queues: dict[int, asyncio.Queue[Any]] = _int_queue_dict()
        # Tasks for progressive call handlers: request_id -> running handler task
        self._progressive_input_tasks: dict[int, asyncio.Task[Any]] = _int_task_dict()

        # Running handler tasks for client CALLs: request_id -> task
        # Used by CANCEL to cancel in-flight handlers
        self._running_call_tasks: dict[int, asyncio.Task[Any]] = _int_task_dict()

        # Request IDs where ERROR was already sent by CANCEL handler
        # (skip/killnowait modes); handler should suppress RESULT/ERROR
        self._cancelled_request_ids: set[int] = set()

        # Counter for server-initiated request IDs (separate from client IDs)
        self._request_id_counter: int = 0

        # Counter for publication IDs (unique and incrementing per session)
        self._publication_id_counter: int = 0

    @property
    def websocket(self) -> WebSocket:
        """The underlying WebSocket connection."""
        return self._websocket

    @property
    def serializer(self) -> Serializer:
        """The serializer used for this session."""
        return self._serializer

    def next_request_id(self) -> int:
        """Generate the next unique request ID for server-initiated messages."""
        self._request_id_counter += 1
        return self._request_id_counter

    # ------------------------------------------------------------------
    # Message I/O
    # ------------------------------------------------------------------

    async def send_message(self, msg_list: list[Any]) -> None:
        """Serialize and send a WAMP message list over the WebSocket.

        Uses text mode for non-binary serializers and binary mode otherwise.
        """
        encoded = self._serializer.encode(msg_list)
        if self._serializer.is_binary:
            if isinstance(encoded, str):
                encoded = encoded.encode("utf-8")
            await self._websocket.send_bytes(encoded)
        else:
            if isinstance(encoded, bytes):
                encoded = encoded.decode("utf-8")
            await self._websocket.send_text(encoded)
        logger.debug("Session %d sent: %s", self.session_id, msg_list)

    async def receive_message(self) -> list[Any]:
        """Read one message from the WebSocket and deserialize it.

        Returns the deserialized WAMP message as a list.
        """
        if self._serializer.is_binary:
            raw: str | bytes = await self._websocket.receive_bytes()
        else:
            raw = await self._websocket.receive_text()
        msg: list[Any] = list(self._serializer.decode(raw))
        logger.debug("Session %d received: %s", self.session_id, msg)
        return msg

    # ------------------------------------------------------------------
    # Async iterator support
    # ------------------------------------------------------------------

    def __aiter__(self) -> AsyncIterator[list[Any]]:
        return self

    async def __anext__(self) -> list[Any]:
        try:
            return await self.receive_message()
        except Exception:
            raise StopAsyncIteration

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def generate_session_id(self, existing_ids: set[int] | None = None) -> int:
        """Generate a random session ID in the WAMP range [1, 2^53].

        If *existing_ids* is provided, ensures the generated ID is unique
        among the currently active sessions.
        """
        max_attempts = 100
        for _ in range(max_attempts):
            candidate = random.randint(1, 2**53)
            if existing_ids is None or candidate not in existing_ids:
                self.session_id = candidate
                return candidate
        # Fallback: extremely unlikely we'd reach here
        raise RuntimeError("Could not generate a unique session ID")

    # ------------------------------------------------------------------
    # WAMP Handshake
    # ------------------------------------------------------------------

    async def perform_handshake(
        self,
        hub_realm: str,
        existing_ids: set[int] | None = None,
    ) -> bool:
        """Perform the WAMP handshake (HELLO -> WELCOME).

        1. Receive first message from the client.
        2. Validate it is a HELLO message with the correct realm.
        3. Generate a unique session ID.
        4. Send WELCOME with session ID and role features.

        Returns ``True`` on success, ``False`` if the handshake fails
        (ABORT is sent automatically).
        """
        try:
            first_msg = await self.receive_message()
        except (WebSocketDisconnect, Exception) as exc:
            logger.warning("Handshake failed: could not receive first message: %s", exc)
            return False

        # First message must be HELLO
        if len(first_msg) == 0:
            await self._send_abort(
                "wamp.error.protocol_violation",
                "First message must be a valid WAMP message",
            )
            return False

        if first_msg[0] != WampMessageType.HELLO:
            await self._send_abort(
                "wamp.error.protocol_violation",
                "First message must be HELLO",
            )
            return False

        # Validate HELLO structure
        try:
            validate_hello(first_msg)
        except WampError as exc:
            await self._send_abort(
                "wamp.error.protocol_violation",
                str(exc),
            )
            return False

        # Validate realm
        client_realm: str = first_msg[1]
        if client_realm != hub_realm:
            await self._send_abort(
                WAMP_ERROR_CLOSE_REALM,
                f"Realm '{client_realm}' does not match hub realm '{hub_realm}'",
            )
            return False

        # Generate unique session ID
        self.generate_session_id(existing_ids)
        self.realm = client_realm
        self.is_open = True

        # Send WELCOME
        welcome_details: dict[str, Any] = {
            "roles": {
                "dealer": {"features": DEALER_FEATURES},
                "broker": {"features": BROKER_FEATURES},
            }
        }
        welcome_msg: list[Any] = [
            WampMessageType.WELCOME,
            self.session_id,
            welcome_details,
        ]
        await self.send_message(welcome_msg)
        logger.info(
            "Session %d: handshake complete (realm=%s)",
            self.session_id,
            self.realm,
        )
        return True

    # ------------------------------------------------------------------
    # Server calling client-registered RPCs
    # ------------------------------------------------------------------

    async def call(
        self,
        uri: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        timeout: float | None = None,
        receive_progress: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> Any:
        """Call a client-registered RPC by URI.

        Looks up *uri* in this session's ``client_rpc_uris`` map, sends an
        INVOCATION to the client, and awaits the YIELD or ERROR response.

        Args:
            uri: The procedure URI to call.
            args: Positional arguments for the invocation.
            kwargs: Keyword arguments for the invocation.
            timeout: Optional timeout in seconds. If the client does not
                respond within this duration, :exc:`WampCallTimeoutError` is raised.
            receive_progress: If ``True``, the INVOCATION is sent with
                ``details.receive_progress = true``, signaling the client
                that it may send progressive YIELD messages.
            on_progress: Optional async callback invoked for each progressive
                YIELD received from the client.  The callback receives the
                intermediate result value.  If ``receive_progress`` is ``True``
                but *on_progress* is not provided, progressive YIELDs are
                silently consumed.

        Returns:
            The result value from the client's final YIELD response.

        Raises:
            WampNoSuchProcedureError: If the URI is not registered on this session.
            WampCallTimeoutError: If the client does not respond within *timeout*.
            WampError: If the client responds with an ERROR message.
        """
        # Look up URI in this session's client RPC map
        registration_id = self.client_rpc_uris.get(uri)
        if registration_id is None:
            raise WampNoSuchProcedureError(f"Procedure '{uri}' not registered on session {self.session_id}")

        request_id = self.next_request_id()

        # Create a Future for the response
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self.pending_calls[request_id] = future

        # Store progress callback if provided
        if receive_progress and on_progress is not None:
            self._pending_progress_callbacks[request_id] = on_progress

        # Build INVOCATION message
        details: dict[str, Any] = {}
        if receive_progress:
            details["receive_progress"] = True

        invocation_msg: list[Any] = [
            WampMessageType.INVOCATION,
            request_id,
            registration_id,
            details,
        ]
        if kwargs:
            invocation_msg.append(args or [])
            invocation_msg.append(kwargs)
        elif args:
            invocation_msg.append(args)

        await self.send_message(invocation_msg)
        logger.debug(
            "Session %d: sent INVOCATION for '%s' (request_id=%d, progressive=%s)",
            self.session_id,
            uri,
            request_id,
            receive_progress,
        )

        # Await the response with optional timeout
        try:
            if timeout is not None and timeout > 0:
                result = await asyncio.wait_for(future, timeout=timeout)
            else:
                result = await future
        except TimeoutError:
            # Clean up the pending call
            self.pending_calls.pop(request_id, None)
            self._pending_progress_callbacks.pop(request_id, None)
            raise WampCallTimeoutError(f"Call to '{uri}' on session {self.session_id} timed out after {timeout}s")
        finally:
            # Always clean up if still present
            self.pending_calls.pop(request_id, None)
            self._pending_progress_callbacks.pop(request_id, None)

        return result

    async def cancel(self, request_id: int, mode: str = "kill") -> None:
        """Cancel a pending server-to-client call.

        Sends a CANCEL message to the client for the given *request_id*.

        The WAMP spec uses INTERRUPT to cancel an INVOCATION that is in
        progress on the callee side.  However, **wampy.js does not handle
        INTERRUPT** on the callee side, so the client may not abort
        mid-execution.  The server simply sends CANCEL to signal intent;
        the client may respond with ERROR ``wamp.error.canceled`` or
        may complete the call normally (in which case the result is
        returned as usual).

        Args:
            request_id: The request ID of the pending INVOCATION.
            mode: Cancel mode — ``"kill"`` (default), ``"skip"``, or
                ``"killnowait"``.  This is passed in the options dict
                of the CANCEL message.

        Note:
            **Known wampy.js limitation:** the callee side does not
            handle INTERRUPT, so the client may not actually abort the
            procedure execution.  The server sends CANCEL/INTERRUPT as
            a hint, but must still be prepared for a normal YIELD.
        """
        if request_id not in self.pending_calls:
            logger.debug(
                "Session %d: cancel(%d) — no pending call, ignoring",
                self.session_id,
                request_id,
            )
            return

        cancel_msg: list[Any] = [
            WampMessageType.CANCEL,
            request_id,
            {"mode": mode},
        ]
        await self.send_message(cancel_msg)
        logger.info(
            "Session %d: sent CANCEL for request %d (mode=%s)",
            self.session_id,
            request_id,
            mode,
        )

    # ------------------------------------------------------------------
    # PubSub: server publishes events to client
    # ------------------------------------------------------------------

    def _next_publication_id(self) -> int:
        """Generate the next unique publication ID for this session."""
        self._publication_id_counter += 1  # XXX: What about overflow?
        return self._publication_id_counter

    async def publish(
        self,
        topic: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        publisher: int | None = None,
    ) -> None:
        """Publish an event to this client session if it is subscribed to *topic*.

        Sends an EVENT message to the client for the given topic.  If the
        client has not subscribed to *topic*, the publishing is silently
        ignored (no error).

        Args:
            topic: The topic URI to publish to.
            args: Positional arguments for the event payload.
            kwargs: Keyword arguments for the event payload.
            publisher: Optional publisher session ID to include in the
                event details (publisher_identification feature).

        EVENT format:
            [EVENT, subscription_id, publication_id, details, args?, kwargs?]
        """
        subscription_id = self.subscription_uris.get(topic)
        if subscription_id is None:
            # Client not subscribed to this topic — no-op
            return

        publication_id = self._next_publication_id()

        details: dict[str, Any] = {}
        if publisher is not None:
            details["publisher"] = publisher

        event_msg: list[Any] = [
            WampMessageType.EVENT,
            subscription_id,
            publication_id,
            details,
        ]
        if kwargs:
            event_msg.append(args or [])
            event_msg.append(kwargs)
        elif args:
            event_msg.append(args)

        await self.send_message(event_msg)
        logger.debug(
            "Session %d: published event on '%s' (publication_id=%d)",
            self.session_id,
            topic,
            publication_id,
        )

    def get_progress_callback(self, request_id: int) -> ProgressCallback | None:
        """Return the on_progress callback for *request_id*, if any."""
        return self._pending_progress_callbacks.get(request_id)

    # ------------------------------------------------------------------
    # Progressive call input (client streams chunks to server)
    # ------------------------------------------------------------------

    def has_progressive_input(self, request_id: int) -> bool:
        """Check if a progressive input stream is active for *request_id*."""
        return request_id in self._progressive_input_queues

    def create_progressive_input(self, request_id: int) -> tuple[asyncio.Queue[Any], ProgressiveCallInput]:
        """Create a new progressive input queue and iterator for *request_id*.

        Returns ``(queue, iterator)`` where *queue* is used by the hub to
        push incoming chunks and *iterator* is passed to the RPC handler.
        """
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._progressive_input_queues[request_id] = queue
        return queue, ProgressiveCallInput(queue)

    def get_progressive_input_queue(self, request_id: int) -> asyncio.Queue[Any] | None:
        """Return the progressive input queue for *request_id*, if any."""
        return self._progressive_input_queues.get(request_id)

    def store_progressive_input_task(self, request_id: int, task: asyncio.Task[Any]) -> None:
        """Store the handler task for a progressive call input."""
        self._progressive_input_tasks[request_id] = task

    def cleanup_progressive_input(self, request_id: int) -> None:
        """Clean up progressive input state for *request_id*."""
        self._progressive_input_queues.pop(request_id, None)
        self._progressive_input_tasks.pop(request_id, None)

    # ------------------------------------------------------------------
    # Running call task tracking (for CANCEL support)
    # ------------------------------------------------------------------

    def store_running_call_task(self, request_id: int, task: asyncio.Task[Any]) -> None:
        """Store a running handler task for a client CALL.

        Used by the hub to track in-flight handler tasks so they can be
        canceled when a CANCEL message arrives.
        """
        self._running_call_tasks[request_id] = task

    def get_running_call_task(self, request_id: int) -> asyncio.Task[Any] | None:
        """Return the running handler task for *request_id*, if any."""
        return self._running_call_tasks.get(request_id)

    def remove_running_call_task(self, request_id: int) -> None:
        """Remove the running handler task for *request_id*."""
        self._running_call_tasks.pop(request_id, None)

    def get_all_running_call_tasks(self) -> list[asyncio.Task[Any]]:
        """Return all currently running call handler tasks."""
        return list(self._running_call_tasks.values())

    def mark_request_cancelled(self, request_id: int) -> None:
        """Mark *request_id* as canceled (ERROR already sent by CANCEL handler).

        The RPC handler should suppress sending RESULT/ERROR for this request.
        """
        self._cancelled_request_ids.add(request_id)

    def is_request_cancelled(self, request_id: int) -> bool:
        """Check whether *request_id* has been canceled (ERROR already sent)."""
        return request_id in self._cancelled_request_ids

    def clear_cancelled_request(self, request_id: int) -> None:
        """Remove *request_id* from the cancelled set."""
        self._cancelled_request_ids.discard(request_id)

    def resolve_pending_call(self, request_id: int, result: Any) -> None:
        """Resolve a pending call future with a successful result.

        Called by the hub when a YIELD message is received from the client.
        """
        future = self.pending_calls.get(request_id)
        if future is not None and not future.done():
            future.set_result(result)
            logger.debug(
                "Session %d: resolved pending call %d",
                self.session_id,
                request_id,
            )

    def reject_pending_call(self, request_id: int, error: Exception) -> None:
        """Reject a pending call future with an error.

        Called by the hub when an ERROR message is received for an INVOCATION.
        """
        future = self.pending_calls.get(request_id)
        if future is not None and not future.done():
            future.set_exception(error)
            logger.debug(
                "Session %d: rejected pending call %d with %s",
                self.session_id,
                request_id,
                type(error).__name__,
            )

    async def handle_goodbye(self, msg: list[Any]) -> None:
        """Handle a GOODBYE message from the client.

        Sends GOODBYE back with ``wamp.error.goodbye_and_out`` and
        marks the session as closed.
        """
        logger.info(
            "Session %d: received GOODBYE, reason=%s",
            self.session_id,
            msg[2] if len(msg) > 2 else "unknown",
        )
        goodbye_reply: list[Any] = [
            WampMessageType.GOODBYE,
            {},
            WAMP_ERROR_GOODBYE_AND_OUT,
        ]
        await self.send_message(goodbye_reply)
        self.is_open = False

    async def _send_abort(self, reason: str, message: str) -> None:
        """Send an ABORT message to the client."""
        abort_msg: list[Any] = [
            WampMessageType.ABORT,
            {"message": message},
            reason,
        ]
        try:
            await self.send_message(abort_msg)
        except Exception as exc:
            logger.debug("Could not send ABORT: %s", exc)
        self.is_open = False

    def cleanup(self) -> None:
        """Clean up session state.

        Clears all maps, rejects pending call futures, cancels progressive
        input tasks, and marks the session as closed.
        """
        self.is_open = False

        # Reject all pending call futures
        for request_id, future in self.pending_calls.items():
            if not future.done():
                future.set_exception(WampError(f"Session {self.session_id} disconnected"))
                logger.debug(
                    "Session %d: rejected pending call %d",
                    self.session_id,
                    request_id,
                )

        # Cancel all running call handler tasks
        for request_id, task in self._running_call_tasks.items():
            if not task.done():
                task.cancel()
                logger.debug(
                    "Session %d: cancelled running call task %d",
                    self.session_id,
                    request_id,
                )

        # Cancel all progressive input tasks and signal end to queues
        for request_id, task in self._progressive_input_tasks.items():
            if not task.done():
                task.cancel()
                logger.debug(
                    "Session %d: cancelled progressive input task %d",
                    self.session_id,
                    request_id,
                )
        for queue in self._progressive_input_queues.values():
            # Signal end so any waiting handler unblocks
            queue.put_nowait(PROGRESSIVE_INPUT_END)

        # Clear all state maps
        self.server_rpcs.clear()
        self.client_rpcs.clear()
        self.client_rpc_uris.clear()
        self.subscriptions.clear()
        self.subscription_uris.clear()
        self.server_subscriptions.clear()
        self.pending_calls.clear()
        self._pending_progress_callbacks.clear()
        self._progressive_input_queues.clear()
        self._progressive_input_tasks.clear()
        self._running_call_tasks.clear()
        self._cancelled_request_ids.clear()

        logger.debug("Session %d: cleanup complete", self.session_id)
