"""WAMP protocol constants, message types, and validation."""

from enum import IntEnum
from typing import Any, cast

from fastapi_headless_wamp.errors import WampInvalidMessage


# ---------------------------------------------------------------------------
# WAMP message type codes (RFC-compliant IntEnum)
# ---------------------------------------------------------------------------


class WampMessageType(IntEnum):
    """WAMP message type codes as defined in the WAMP specification."""

    HELLO = 1
    WELCOME = 2
    ABORT = 3
    GOODBYE = 6
    ERROR = 8
    PUBLISH = 16
    PUBLISHED = 17
    SUBSCRIBE = 32
    SUBSCRIBED = 33
    UNSUBSCRIBE = 34
    UNSUBSCRIBED = 35
    EVENT = 36
    CALL = 48
    CANCEL = 49
    RESULT = 50
    REGISTER = 64
    REGISTERED = 65
    UNREGISTER = 66
    UNREGISTERED = 67
    INVOCATION = 68
    INTERRUPT = 69
    YIELD = 70


# ---------------------------------------------------------------------------
# WAMP error URI constants
# ---------------------------------------------------------------------------

WAMP_ERROR_NO_SUCH_PROCEDURE = "wamp.error.no_such_procedure"
WAMP_ERROR_INVALID_ARGUMENT = "wamp.error.invalid_argument"
WAMP_ERROR_RUNTIME_ERROR = "wamp.error.runtime_error"
WAMP_ERROR_NO_SUCH_SUBSCRIPTION = "wamp.error.no_such_subscription"
WAMP_ERROR_PROCEDURE_ALREADY_EXISTS = "wamp.error.procedure_already_exists"
WAMP_ERROR_CANCELED = "wamp.error.canceled"
WAMP_ERROR_SYSTEM_SHUTDOWN = "wamp.error.system_shutdown"
WAMP_ERROR_CLOSE_REALM = "wamp.error.close_realm"
WAMP_ERROR_GOODBYE_AND_OUT = "wamp.error.goodbye_and_out"


# ---------------------------------------------------------------------------
# Message validation helpers
# ---------------------------------------------------------------------------


def _check_min_length(msg: list[Any], min_len: int, msg_type: str) -> None:
    """Raise WampInvalidMessage if *msg* is shorter than *min_len*."""
    if len(msg) < min_len:
        raise WampInvalidMessage(
            f"{msg_type} message must have at least {min_len} elements, got {len(msg)}"
        )


def _check_type(
    value: Any, expected: type | tuple[type, ...], field: str, msg_type: str
) -> None:
    """Raise WampInvalidMessage if *value* is not the expected type."""
    if not isinstance(value, expected):
        raise WampInvalidMessage(
            f"{msg_type} field '{field}' must be {expected}, got {type(value).__name__}"
        )


# ---------------------------------------------------------------------------
# Per-message-type validators
# ---------------------------------------------------------------------------


def validate_hello(msg: list[Any]) -> None:
    """Validate HELLO [HELLO, Realm|uri, Details|dict]."""
    _check_min_length(msg, 3, "HELLO")
    _check_type(msg[1], str, "realm", "HELLO")
    _check_type(msg[2], dict, "details", "HELLO")


def validate_welcome(msg: list[Any]) -> None:
    """Validate WELCOME [WELCOME, Session|id, Details|dict]."""
    _check_min_length(msg, 3, "WELCOME")
    _check_type(msg[1], int, "session", "WELCOME")
    _check_type(msg[2], dict, "details", "WELCOME")


def validate_abort(msg: list[Any]) -> None:
    """Validate ABORT [ABORT, Details|dict, Reason|uri]."""
    _check_min_length(msg, 3, "ABORT")
    _check_type(msg[1], dict, "details", "ABORT")
    _check_type(msg[2], str, "reason", "ABORT")


def validate_goodbye(msg: list[Any]) -> None:
    """Validate GOODBYE [GOODBYE, Details|dict, Reason|uri]."""
    _check_min_length(msg, 3, "GOODBYE")
    _check_type(msg[1], dict, "details", "GOODBYE")
    _check_type(msg[2], str, "reason", "GOODBYE")


def validate_error(msg: list[Any]) -> None:
    """Validate ERROR [ERROR, REQUEST.Type|int, REQUEST.Request|id, Details|dict, Error|uri, Arguments|list?, ArgumentsKw|dict?]."""
    _check_min_length(msg, 5, "ERROR")
    _check_type(msg[1], int, "request_type", "ERROR")
    _check_type(msg[2], int, "request_id", "ERROR")
    _check_type(msg[3], dict, "details", "ERROR")
    _check_type(msg[4], str, "error", "ERROR")
    if len(msg) > 5:
        _check_type(msg[5], list, "arguments", "ERROR")
    if len(msg) > 6:
        _check_type(msg[6], dict, "arguments_kw", "ERROR")


def validate_publish(msg: list[Any]) -> None:
    """Validate PUBLISH [PUBLISH, Request|id, Options|dict, Topic|uri, Arguments|list?, ArgumentsKw|dict?]."""
    _check_min_length(msg, 4, "PUBLISH")
    _check_type(msg[1], int, "request_id", "PUBLISH")
    _check_type(msg[2], dict, "options", "PUBLISH")
    _check_type(msg[3], str, "topic", "PUBLISH")
    if len(msg) > 4:
        _check_type(msg[4], list, "arguments", "PUBLISH")
    if len(msg) > 5:
        _check_type(msg[5], dict, "arguments_kw", "PUBLISH")


def validate_published(msg: list[Any]) -> None:
    """Validate PUBLISHED [PUBLISHED, PUBLISH.Request|id, Publication|id]."""
    _check_min_length(msg, 3, "PUBLISHED")
    _check_type(msg[1], int, "request_id", "PUBLISHED")
    _check_type(msg[2], int, "publication_id", "PUBLISHED")


def validate_subscribe(msg: list[Any]) -> None:
    """Validate SUBSCRIBE [SUBSCRIBE, Request|id, Options|dict, Topic|uri]."""
    _check_min_length(msg, 4, "SUBSCRIBE")
    _check_type(msg[1], int, "request_id", "SUBSCRIBE")
    _check_type(msg[2], dict, "options", "SUBSCRIBE")
    _check_type(msg[3], str, "topic", "SUBSCRIBE")


def validate_subscribed(msg: list[Any]) -> None:
    """Validate SUBSCRIBED [SUBSCRIBED, SUBSCRIBE.Request|id, Subscription|id]."""
    _check_min_length(msg, 3, "SUBSCRIBED")
    _check_type(msg[1], int, "request_id", "SUBSCRIBED")
    _check_type(msg[2], int, "subscription_id", "SUBSCRIBED")


def validate_unsubscribe(msg: list[Any]) -> None:
    """Validate UNSUBSCRIBE [UNSUBSCRIBE, Request|id, SUBSCRIBED.Subscription|id]."""
    _check_min_length(msg, 3, "UNSUBSCRIBE")
    _check_type(msg[1], int, "request_id", "UNSUBSCRIBE")
    _check_type(msg[2], int, "subscription_id", "UNSUBSCRIBE")


def validate_unsubscribed(msg: list[Any]) -> None:
    """Validate UNSUBSCRIBED [UNSUBSCRIBED, UNSUBSCRIBE.Request|id]."""
    _check_min_length(msg, 2, "UNSUBSCRIBED")
    _check_type(msg[1], int, "request_id", "UNSUBSCRIBED")


def validate_event(msg: list[Any]) -> None:
    """Validate EVENT [EVENT, SUBSCRIBED.Subscription|id, PUBLISHED.Publication|id, Details|dict, Arguments|list?, ArgumentsKw|dict?]."""
    _check_min_length(msg, 4, "EVENT")
    _check_type(msg[1], int, "subscription_id", "EVENT")
    _check_type(msg[2], int, "publication_id", "EVENT")
    _check_type(msg[3], dict, "details", "EVENT")
    if len(msg) > 4:
        _check_type(msg[4], list, "arguments", "EVENT")
    if len(msg) > 5:
        _check_type(msg[5], dict, "arguments_kw", "EVENT")


def validate_call(msg: list[Any]) -> None:
    """Validate CALL [CALL, Request|id, Options|dict, Procedure|uri, Arguments|list?, ArgumentsKw|dict?]."""
    _check_min_length(msg, 4, "CALL")
    _check_type(msg[1], int, "request_id", "CALL")
    _check_type(msg[2], dict, "options", "CALL")
    _check_type(msg[3], str, "procedure", "CALL")
    if len(msg) > 4:
        _check_type(msg[4], list, "arguments", "CALL")
    if len(msg) > 5:
        _check_type(msg[5], dict, "arguments_kw", "CALL")


def validate_cancel(msg: list[Any]) -> None:
    """Validate CANCEL [CANCEL, CALL.Request|id, Options|dict]."""
    _check_min_length(msg, 3, "CANCEL")
    _check_type(msg[1], int, "request_id", "CANCEL")
    _check_type(msg[2], dict, "options", "CANCEL")


def validate_result(msg: list[Any]) -> None:
    """Validate RESULT [RESULT, CALL.Request|id, Details|dict, YIELD.Arguments|list?, YIELD.ArgumentsKw|dict?]."""
    _check_min_length(msg, 3, "RESULT")
    _check_type(msg[1], int, "request_id", "RESULT")
    _check_type(msg[2], dict, "details", "RESULT")
    if len(msg) > 3:
        _check_type(msg[3], list, "arguments", "RESULT")
    if len(msg) > 4:
        _check_type(msg[4], dict, "arguments_kw", "RESULT")


def validate_register(msg: list[Any]) -> None:
    """Validate REGISTER [REGISTER, Request|id, Options|dict, Procedure|uri]."""
    _check_min_length(msg, 4, "REGISTER")
    _check_type(msg[1], int, "request_id", "REGISTER")
    _check_type(msg[2], dict, "options", "REGISTER")
    _check_type(msg[3], str, "procedure", "REGISTER")


def validate_registered(msg: list[Any]) -> None:
    """Validate REGISTERED [REGISTERED, REGISTER.Request|id, Registration|id]."""
    _check_min_length(msg, 3, "REGISTERED")
    _check_type(msg[1], int, "request_id", "REGISTERED")
    _check_type(msg[2], int, "registration_id", "REGISTERED")


def validate_unregister(msg: list[Any]) -> None:
    """Validate UNREGISTER [UNREGISTER, Request|id, REGISTERED.Registration|id]."""
    _check_min_length(msg, 3, "UNREGISTER")
    _check_type(msg[1], int, "request_id", "UNREGISTER")
    _check_type(msg[2], int, "registration_id", "UNREGISTER")


def validate_unregistered(msg: list[Any]) -> None:
    """Validate UNREGISTERED [UNREGISTERED, UNREGISTER.Request|id]."""
    _check_min_length(msg, 2, "UNREGISTERED")
    _check_type(msg[1], int, "request_id", "UNREGISTERED")


def validate_invocation(msg: list[Any]) -> None:
    """Validate INVOCATION [INVOCATION, Request|id, REGISTERED.Registration|id, Details|dict, Arguments|list?, ArgumentsKw|dict?]."""
    _check_min_length(msg, 4, "INVOCATION")
    _check_type(msg[1], int, "request_id", "INVOCATION")
    _check_type(msg[2], int, "registration_id", "INVOCATION")
    _check_type(msg[3], dict, "details", "INVOCATION")
    if len(msg) > 4:
        _check_type(msg[4], list, "arguments", "INVOCATION")
    if len(msg) > 5:
        _check_type(msg[5], dict, "arguments_kw", "INVOCATION")


def validate_interrupt(msg: list[Any]) -> None:
    """Validate INTERRUPT [INTERRUPT, INVOCATION.Request|id, Options|dict]."""
    _check_min_length(msg, 3, "INTERRUPT")
    _check_type(msg[1], int, "request_id", "INTERRUPT")
    _check_type(msg[2], dict, "options", "INTERRUPT")


def validate_yield(msg: list[Any]) -> None:
    """Validate YIELD [YIELD, INVOCATION.Request|id, Options|dict, Arguments|list?, ArgumentsKw|dict?]."""
    _check_min_length(msg, 3, "YIELD")
    _check_type(msg[1], int, "request_id", "YIELD")
    _check_type(msg[2], dict, "options", "YIELD")
    if len(msg) > 3:
        _check_type(msg[3], list, "arguments", "YIELD")
    if len(msg) > 4:
        _check_type(msg[4], dict, "arguments_kw", "YIELD")


# ---------------------------------------------------------------------------
# Validator dispatch table
# ---------------------------------------------------------------------------

_VALIDATORS: dict[WampMessageType, Any] = {
    WampMessageType.HELLO: validate_hello,
    WampMessageType.WELCOME: validate_welcome,
    WampMessageType.ABORT: validate_abort,
    WampMessageType.GOODBYE: validate_goodbye,
    WampMessageType.ERROR: validate_error,
    WampMessageType.PUBLISH: validate_publish,
    WampMessageType.PUBLISHED: validate_published,
    WampMessageType.SUBSCRIBE: validate_subscribe,
    WampMessageType.SUBSCRIBED: validate_subscribed,
    WampMessageType.UNSUBSCRIBE: validate_unsubscribe,
    WampMessageType.UNSUBSCRIBED: validate_unsubscribed,
    WampMessageType.EVENT: validate_event,
    WampMessageType.CALL: validate_call,
    WampMessageType.CANCEL: validate_cancel,
    WampMessageType.RESULT: validate_result,
    WampMessageType.REGISTER: validate_register,
    WampMessageType.REGISTERED: validate_registered,
    WampMessageType.UNREGISTER: validate_unregister,
    WampMessageType.UNREGISTERED: validate_unregistered,
    WampMessageType.INVOCATION: validate_invocation,
    WampMessageType.INTERRUPT: validate_interrupt,
    WampMessageType.YIELD: validate_yield,
}


def validate_message(msg: Any) -> None:
    """Validate a WAMP message array.

    Checks that the message is a non-empty list, the first element is a valid
    message type code, and delegates to the per-type validator.

    Raises:
        WampInvalidMessage: If the message is malformed.
    """
    if not isinstance(msg, list):
        raise WampInvalidMessage("WAMP message must be a non-empty list")

    raw_list = cast(list[Any], msg)
    if len(raw_list) == 0:
        raise WampInvalidMessage("WAMP message must be a non-empty list")

    try:
        msg_type = WampMessageType(raw_list[0])
    except ValueError:
        raise WampInvalidMessage(f"Unknown WAMP message type code: {raw_list[0]}")

    validator = _VALIDATORS.get(msg_type)
    if validator is not None:
        validator(raw_list)
