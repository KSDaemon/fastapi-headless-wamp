"""fastapi-headless-wamp: Bidirectional WAMP RPC and PubSub for FastAPI."""

import logging

from fastapi_headless_wamp.errors import (
    WampCallTimeoutError,
    WampCanceledError,
    WampError,
    WampInvalidMessageError,
    WampNoSuchProcedureError,
    WampNoSuchSubscriptionError,
    WampProcedureAlreadyExistsError,
    WampProtocolError,
    WampRuntimeError,
)
from fastapi_headless_wamp.hub import WampHub
from fastapi_headless_wamp.serializers import (
    JsonSerializer,
    Serializer,
    get_available_subprotocols,
    get_serializer,
    register_serializer,
)
from fastapi_headless_wamp.service import WampService, rpc, subscribe
from fastapi_headless_wamp.session import ProgressiveCallInput, WampSession

# Configure logging with NullHandler to prevent "No handler found" warnings
logging.getLogger("fastapi_headless_wamp").addHandler(logging.NullHandler())

__all__ = [
    "JsonSerializer",
    "ProgressiveCallInput",
    "Serializer",
    "WampCallTimeoutError",
    "WampCanceledError",
    "WampError",
    "WampHub",
    "WampInvalidMessageError",
    "WampNoSuchProcedureError",
    "WampNoSuchSubscriptionError",
    "WampProcedureAlreadyExistsError",
    "WampProtocolError",
    "WampRuntimeError",
    "WampService",
    "WampSession",
    "get_available_subprotocols",
    "get_serializer",
    "register_serializer",
    "rpc",
    "subscribe",
]
