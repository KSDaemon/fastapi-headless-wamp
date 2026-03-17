"""Class-based WAMP service with @rpc decorator."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi_headless_wamp.hub import WampHub

logger = logging.getLogger(__name__)


def rpc(uri: str | None = None) -> Callable[..., Any]:
    """Decorator to mark a method as an RPC handler.

    Usage::

        @rpc("my_procedure")
        async def my_handler(self, x: int) -> int:
            return x * 2


        @rpc()  # URI inferred from method name
        async def another_handler(self) -> str:
            return "hello"

    The full URI is constructed as ``prefix + '.' + uri`` (or
    ``prefix + '.' + method_name`` when *uri* is ``None``).
    """

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
    """Base class for grouping related RPCs into a service with a URI prefix.

    Subclass this and decorate methods with :func:`rpc` to define RPC
    handlers.  Register the service instance with a :class:`WampHub` via
    :meth:`WampHub.register_service`.

    Example::

        class MathService(WampService):
            prefix = "com.example.math"

            @rpc()
            async def add(self, a: int, b: int) -> int:
                return a + b

            @rpc("multiply")
            async def mul(self, a: int, b: int) -> int:
                return a * b


        hub = WampHub()
        hub.register_service(MathService())
    """

    prefix: str = ""

    # Set by WampHub.register_service()
    hub: WampHub | None = None
