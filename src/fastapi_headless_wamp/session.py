"""WAMP session wrapping a FastAPI WebSocket."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, AsyncIterator, Callable

from starlette.websockets import WebSocket

from fastapi_headless_wamp.errors import WampError
from fastapi_headless_wamp.serializers import Serializer

logger = logging.getLogger(__name__)

# Type aliases for internal maps
RpcHandler = Callable[..., Any]


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

    def generate_session_id(self) -> int:
        """Generate a random session ID in the WAMP range [1, 2^53]."""
        self.session_id = random.randint(1, 2**53)
        return self.session_id

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
