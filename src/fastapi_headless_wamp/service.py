"""Class-based WAMP service with @rpc decorator."""

from typing import Any
from collections.abc import Callable


def rpc(uri: str | None = None) -> Callable[..., Any]:
    """Decorator to mark a method as an RPC handler."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        func._rpc_uri = uri  # type: ignore[attr-defined]
        return func

    return decorator


def subscribe(topic: str) -> Callable[..., Any]:
    """Decorator to mark a method as a subscription handler."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        func._subscribe_topic = topic  # type: ignore[attr-defined]
        return func

    return decorator


class WampService:
    """Base class for grouping related RPCs into a service with a URI prefix."""

    prefix: str = ""
