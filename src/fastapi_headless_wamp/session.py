"""WAMP session wrapping a FastAPI WebSocket."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, AsyncIterator, Callable

from starlette.websockets import WebSocket, WebSocketDisconnect

from fastapi_headless_wamp.errors import (
    WampCallTimeout,
    WampError,
    WampNoSuchProcedure,
)
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

# WAMP role features advertised by the server
DEALER_FEATURES: dict[str, bool] = {
    "progressive_call_results": True,
    "call_canceling": True,
    "caller_identification": True,
    "call_timeout": True,
}

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

        # Counter for server-initiated request IDs (separate from client IDs)
        self._request_id_counter: int = 0

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
    ) -> Any:
        """Call a client-registered RPC by URI.

        Looks up *uri* in this session's ``client_rpc_uris`` map, sends an
        INVOCATION to the client, and awaits the YIELD or ERROR response.

        Args:
            uri: The procedure URI to call.
            args: Positional arguments for the invocation.
            kwargs: Keyword arguments for the invocation.
            timeout: Optional timeout in seconds. If the client does not
                respond within this duration, :exc:`WampCallTimeout` is raised.

        Returns:
            The result value from the client's YIELD response.

        Raises:
            WampNoSuchProcedure: If the URI is not registered on this session.
            WampCallTimeout: If the client does not respond within *timeout*.
            WampError: If the client responds with an ERROR message.
        """
        # Look up URI in this session's client RPC map
        registration_id = self.client_rpc_uris.get(uri)
        if registration_id is None:
            raise WampNoSuchProcedure(
                f"Procedure '{uri}' not registered on session {self.session_id}"
            )

        request_id = self.next_request_id()

        # Create a Future for the response
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self.pending_calls[request_id] = future

        # Build INVOCATION message
        invocation_msg: list[Any] = [
            WampMessageType.INVOCATION,
            request_id,
            registration_id,
            {},  # details
        ]
        if kwargs:
            invocation_msg.append(args or [])
            invocation_msg.append(kwargs)
        elif args:
            invocation_msg.append(args)

        await self.send_message(invocation_msg)
        logger.debug(
            "Session %d: sent INVOCATION for '%s' (request_id=%d)",
            self.session_id,
            uri,
            request_id,
        )

        # Await the response with optional timeout
        try:
            if timeout is not None and timeout > 0:
                result = await asyncio.wait_for(future, timeout=timeout)
            else:
                result = await future
        except asyncio.TimeoutError:
            # Clean up the pending call
            self.pending_calls.pop(request_id, None)
            raise WampCallTimeout(
                f"Call to '{uri}' on session {self.session_id} timed out "
                f"after {timeout}s"
            )
        finally:
            # Always clean up if still present
            self.pending_calls.pop(request_id, None)

        return result

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

        Clears all maps, rejects pending call futures, and marks the
        session as closed.
        """
        self.is_open = False

        # Reject all pending call futures
        for request_id, future in self.pending_calls.items():
            if not future.done():
                future.set_exception(
                    WampError(f"Session {self.session_id} disconnected")
                )
                logger.debug(
                    "Session %d: rejected pending call %d",
                    self.session_id,
                    request_id,
                )

        # Clear all state maps
        self.server_rpcs.clear()
        self.client_rpcs.clear()
        self.client_rpc_uris.clear()
        self.subscriptions.clear()
        self.subscription_uris.clear()
        self.server_subscriptions.clear()
        self.pending_calls.clear()

        logger.debug("Session %d: cleanup complete", self.session_id)
