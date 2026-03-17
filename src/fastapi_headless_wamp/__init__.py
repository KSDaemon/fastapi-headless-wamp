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
    # Core classes
    "WampHub",
    "WampSession",
    "WampService",
    "ProgressiveCallInput",
    # Decorators
    "rpc",
    "subscribe",
    # Serializers
    "Serializer",
    "JsonSerializer",
    "register_serializer",
    "get_serializer",
    "get_available_subprotocols",
    # Errors
    "WampError",
    "WampProtocolError",
    "WampInvalidMessageError",
    "WampNoSuchProcedureError",
    "WampNoSuchSubscriptionError",
    "WampRuntimeError",
    "WampCallTimeoutError",
    "WampCanceledError",
    "WampProcedureAlreadyExistsError",
]
