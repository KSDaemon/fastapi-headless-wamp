"""WAMP error hierarchy."""

from typing import Any


class WampError(Exception):
    """Base class for all WAMP errors."""

    uri: str = "wamp.error"

    def __init__(
        self,
        message: str = "",
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.wamp_args = args or []
        self.wamp_kwargs = kwargs or {}


class WampProtocolError(WampError):
    """WAMP protocol-level error."""

    uri: str = "wamp.error.protocol_error"


class WampInvalidMessageError(WampError):
    """Invalid WAMP message."""

    uri: str = "wamp.error.invalid_message"


class WampNoSuchProcedureError(WampError):
    """No such procedure registered."""

    uri: str = "wamp.error.no_such_procedure"


class WampNoSuchSubscriptionError(WampError):
    """No such subscription exists."""

    uri: str = "wamp.error.no_such_subscription"


class WampRuntimeError(WampError):
    """Runtime error during RPC execution."""

    uri: str = "wamp.error.runtime_error"


class WampCallTimeoutError(WampError):
    """Call timed out waiting for response."""

    uri: str = "wamp.error.canceled"


class WampCanceledError(WampError):
    """Call was canceled."""

    uri: str = "wamp.error.canceled"


class WampProcedureAlreadyExistsError(WampError):
    """Procedure is already registered."""

    uri: str = "wamp.error.procedure_already_exists"
