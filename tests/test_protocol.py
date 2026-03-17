"""Tests for WAMP protocol constants and message validation."""

import pytest

from fastapi_headless_wamp.errors import WampInvalidMessage
from fastapi_headless_wamp.protocol import (
    WAMP_ERROR_CANCELED,
    WAMP_ERROR_CLOSE_REALM,
    WAMP_ERROR_GOODBYE_AND_OUT,
    WAMP_ERROR_INVALID_ARGUMENT,
    WAMP_ERROR_NO_SUCH_PROCEDURE,
    WAMP_ERROR_NO_SUCH_SUBSCRIPTION,
    WAMP_ERROR_PROCEDURE_ALREADY_EXISTS,
    WAMP_ERROR_RUNTIME_ERROR,
    WAMP_ERROR_SYSTEM_SHUTDOWN,
    WampMessageType,
    validate_abort,
    validate_call,
    validate_cancel,
    validate_error,
    validate_event,
    validate_goodbye,
    validate_hello,
    validate_interrupt,
    validate_invocation,
    validate_message,
    validate_publish,
    validate_published,
    validate_register,
    validate_registered,
    validate_result,
    validate_subscribe,
    validate_subscribed,
    validate_unregister,
    validate_unregistered,
    validate_unsubscribe,
    validate_unsubscribed,
    validate_welcome,
    validate_yield,
)
from fastapi_headless_wamp.types import (
    CallMessage,
    GoodbyeMessage,
    HelloMessage,
    ResultMessage,
    WelcomeMessage,
)

# ---------------------------------------------------------------------------
# WampMessageType IntEnum tests
# ---------------------------------------------------------------------------


class TestWampMessageType:
    """Test WAMP message type code enum."""

    def test_hello_code(self) -> None:
        assert WampMessageType.HELLO == 1

    def test_welcome_code(self) -> None:
        assert WampMessageType.WELCOME == 2

    def test_abort_code(self) -> None:
        assert WampMessageType.ABORT == 3

    def test_goodbye_code(self) -> None:
        assert WampMessageType.GOODBYE == 6

    def test_error_code(self) -> None:
        assert WampMessageType.ERROR == 8

    def test_publish_code(self) -> None:
        assert WampMessageType.PUBLISH == 16

    def test_published_code(self) -> None:
        assert WampMessageType.PUBLISHED == 17

    def test_subscribe_code(self) -> None:
        assert WampMessageType.SUBSCRIBE == 32

    def test_subscribed_code(self) -> None:
        assert WampMessageType.SUBSCRIBED == 33

    def test_unsubscribe_code(self) -> None:
        assert WampMessageType.UNSUBSCRIBE == 34

    def test_unsubscribed_code(self) -> None:
        assert WampMessageType.UNSUBSCRIBED == 35

    def test_event_code(self) -> None:
        assert WampMessageType.EVENT == 36

    def test_call_code(self) -> None:
        assert WampMessageType.CALL == 48

    def test_cancel_code(self) -> None:
        assert WampMessageType.CANCEL == 49

    def test_result_code(self) -> None:
        assert WampMessageType.RESULT == 50

    def test_register_code(self) -> None:
        assert WampMessageType.REGISTER == 64

    def test_registered_code(self) -> None:
        assert WampMessageType.REGISTERED == 65

    def test_unregister_code(self) -> None:
        assert WampMessageType.UNREGISTER == 66

    def test_unregistered_code(self) -> None:
        assert WampMessageType.UNREGISTERED == 67

    def test_invocation_code(self) -> None:
        assert WampMessageType.INVOCATION == 68

    def test_interrupt_code(self) -> None:
        assert WampMessageType.INTERRUPT == 69

    def test_yield_code(self) -> None:
        assert WampMessageType.YIELD == 70

    def test_is_int(self) -> None:
        assert isinstance(WampMessageType.HELLO, int)

    def test_all_members_count(self) -> None:
        assert len(WampMessageType) == 22


# ---------------------------------------------------------------------------
# Error URI constant tests
# ---------------------------------------------------------------------------


class TestWampErrorUris:
    """Test that error URIs match the WAMP spec strings."""

    def test_no_such_procedure(self) -> None:
        assert WAMP_ERROR_NO_SUCH_PROCEDURE == "wamp.error.no_such_procedure"

    def test_invalid_argument(self) -> None:
        assert WAMP_ERROR_INVALID_ARGUMENT == "wamp.error.invalid_argument"

    def test_runtime_error(self) -> None:
        assert WAMP_ERROR_RUNTIME_ERROR == "wamp.error.runtime_error"

    def test_no_such_subscription(self) -> None:
        assert WAMP_ERROR_NO_SUCH_SUBSCRIPTION == "wamp.error.no_such_subscription"

    def test_procedure_already_exists(self) -> None:
        assert (
            WAMP_ERROR_PROCEDURE_ALREADY_EXISTS == "wamp.error.procedure_already_exists"
        )

    def test_canceled(self) -> None:
        assert WAMP_ERROR_CANCELED == "wamp.error.canceled"

    def test_system_shutdown(self) -> None:
        assert WAMP_ERROR_SYSTEM_SHUTDOWN == "wamp.error.system_shutdown"

    def test_close_realm(self) -> None:
        assert WAMP_ERROR_CLOSE_REALM == "wamp.error.close_realm"

    def test_goodbye_and_out(self) -> None:
        assert WAMP_ERROR_GOODBYE_AND_OUT == "wamp.error.goodbye_and_out"


# ---------------------------------------------------------------------------
# Message validation: valid messages
# ---------------------------------------------------------------------------


class TestValidateValidMessages:
    """Test that valid WAMP messages pass validation."""

    def test_hello(self) -> None:
        validate_hello([WampMessageType.HELLO, "realm1", {}])

    def test_welcome(self) -> None:
        validate_welcome([WampMessageType.WELCOME, 12345, {"roles": {}}])

    def test_abort(self) -> None:
        validate_abort([WampMessageType.ABORT, {}, "wamp.error.no_such_realm"])

    def test_goodbye(self) -> None:
        validate_goodbye([WampMessageType.GOODBYE, {}, "wamp.error.close_realm"])

    def test_error_minimal(self) -> None:
        validate_error([WampMessageType.ERROR, 48, 1, {}, "wamp.error.runtime_error"])

    def test_error_with_args(self) -> None:
        validate_error(
            [WampMessageType.ERROR, 48, 1, {}, "wamp.error.runtime_error", ["arg1"]]
        )

    def test_error_with_args_kwargs(self) -> None:
        validate_error(
            [
                WampMessageType.ERROR,
                48,
                1,
                {},
                "wamp.error.runtime_error",
                ["arg1"],
                {"key": "val"},
            ]
        )

    def test_publish_minimal(self) -> None:
        validate_publish([WampMessageType.PUBLISH, 1, {}, "com.example.topic"])

    def test_publish_with_args(self) -> None:
        validate_publish(
            [WampMessageType.PUBLISH, 1, {}, "com.example.topic", ["hello"]]
        )

    def test_publish_with_args_kwargs(self) -> None:
        validate_publish(
            [
                WampMessageType.PUBLISH,
                1,
                {},
                "com.example.topic",
                ["hello"],
                {"key": "val"},
            ]
        )

    def test_published(self) -> None:
        validate_published([WampMessageType.PUBLISHED, 1, 100])

    def test_subscribe(self) -> None:
        validate_subscribe([WampMessageType.SUBSCRIBE, 1, {}, "com.example.topic"])

    def test_subscribed(self) -> None:
        validate_subscribed([WampMessageType.SUBSCRIBED, 1, 200])

    def test_unsubscribe(self) -> None:
        validate_unsubscribe([WampMessageType.UNSUBSCRIBE, 1, 200])

    def test_unsubscribed(self) -> None:
        validate_unsubscribed([WampMessageType.UNSUBSCRIBED, 1])

    def test_event_minimal(self) -> None:
        validate_event([WampMessageType.EVENT, 200, 100, {}])

    def test_event_with_args(self) -> None:
        validate_event([WampMessageType.EVENT, 200, 100, {}, ["data"]])

    def test_event_with_args_kwargs(self) -> None:
        validate_event([WampMessageType.EVENT, 200, 100, {}, ["data"], {"extra": 1}])

    def test_call_minimal(self) -> None:
        validate_call([WampMessageType.CALL, 1, {}, "com.example.add"])

    def test_call_with_args(self) -> None:
        validate_call([WampMessageType.CALL, 1, {}, "com.example.add", [1, 2]])

    def test_call_with_args_kwargs(self) -> None:
        validate_call(
            [WampMessageType.CALL, 1, {}, "com.example.add", [1, 2], {"key": "val"}]
        )

    def test_cancel(self) -> None:
        validate_cancel([WampMessageType.CANCEL, 1, {}])

    def test_result_minimal(self) -> None:
        validate_result([WampMessageType.RESULT, 1, {}])

    def test_result_with_args(self) -> None:
        validate_result([WampMessageType.RESULT, 1, {}, [42]])

    def test_result_with_args_kwargs(self) -> None:
        validate_result([WampMessageType.RESULT, 1, {}, [42], {"key": "val"}])

    def test_register(self) -> None:
        validate_register([WampMessageType.REGISTER, 1, {}, "com.example.add"])

    def test_registered(self) -> None:
        validate_registered([WampMessageType.REGISTERED, 1, 300])

    def test_unregister(self) -> None:
        validate_unregister([WampMessageType.UNREGISTER, 1, 300])

    def test_unregistered(self) -> None:
        validate_unregistered([WampMessageType.UNREGISTERED, 1])

    def test_invocation_minimal(self) -> None:
        validate_invocation([WampMessageType.INVOCATION, 1, 300, {}])

    def test_invocation_with_args(self) -> None:
        validate_invocation([WampMessageType.INVOCATION, 1, 300, {}, [1, 2]])

    def test_invocation_with_args_kwargs(self) -> None:
        validate_invocation(
            [WampMessageType.INVOCATION, 1, 300, {}, [1, 2], {"key": "val"}]
        )

    def test_interrupt(self) -> None:
        validate_interrupt([WampMessageType.INTERRUPT, 1, {}])

    def test_yield_minimal(self) -> None:
        validate_yield([WampMessageType.YIELD, 1, {}])

    def test_yield_with_args(self) -> None:
        validate_yield([WampMessageType.YIELD, 1, {}, [42]])

    def test_yield_with_args_kwargs(self) -> None:
        validate_yield([WampMessageType.YIELD, 1, {}, [42], {"key": "val"}])


# ---------------------------------------------------------------------------
# Message validation: invalid messages
# ---------------------------------------------------------------------------


class TestValidateInvalidMessages:
    """Test that invalid WAMP messages raise WampInvalidMessage."""

    def test_hello_too_short(self) -> None:
        with pytest.raises(WampInvalidMessage):
            validate_hello([WampMessageType.HELLO, "realm1"])

    def test_hello_realm_not_string(self) -> None:
        with pytest.raises(WampInvalidMessage):
            validate_hello([WampMessageType.HELLO, 123, {}])

    def test_hello_details_not_dict(self) -> None:
        with pytest.raises(WampInvalidMessage):
            validate_hello([WampMessageType.HELLO, "realm1", "not_a_dict"])

    def test_welcome_too_short(self) -> None:
        with pytest.raises(WampInvalidMessage):
            validate_welcome([WampMessageType.WELCOME, 12345])

    def test_welcome_session_not_int(self) -> None:
        with pytest.raises(WampInvalidMessage):
            validate_welcome([WampMessageType.WELCOME, "not_int", {}])

    def test_abort_details_not_dict(self) -> None:
        with pytest.raises(WampInvalidMessage):
            validate_abort([WampMessageType.ABORT, "not_dict", "reason"])

    def test_goodbye_too_short(self) -> None:
        with pytest.raises(WampInvalidMessage):
            validate_goodbye([WampMessageType.GOODBYE, {}])

    def test_error_too_short(self) -> None:
        with pytest.raises(WampInvalidMessage):
            validate_error([WampMessageType.ERROR, 48, 1, {}])

    def test_error_args_not_list(self) -> None:
        with pytest.raises(WampInvalidMessage):
            validate_error(
                [
                    WampMessageType.ERROR,
                    48,
                    1,
                    {},
                    "wamp.error.runtime_error",
                    "not_list",
                ]
            )

    def test_error_kwargs_not_dict(self) -> None:
        with pytest.raises(WampInvalidMessage):
            validate_error(
                [
                    WampMessageType.ERROR,
                    48,
                    1,
                    {},
                    "wamp.error.runtime_error",
                    [],
                    "not_dict",
                ]
            )

    def test_publish_too_short(self) -> None:
        with pytest.raises(WampInvalidMessage):
            validate_publish([WampMessageType.PUBLISH, 1, {}])

    def test_publish_topic_not_string(self) -> None:
        with pytest.raises(WampInvalidMessage):
            validate_publish([WampMessageType.PUBLISH, 1, {}, 42])

    def test_subscribe_too_short(self) -> None:
        with pytest.raises(WampInvalidMessage):
            validate_subscribe([WampMessageType.SUBSCRIBE, 1, {}])

    def test_call_procedure_not_string(self) -> None:
        with pytest.raises(WampInvalidMessage):
            validate_call([WampMessageType.CALL, 1, {}, 42])

    def test_cancel_too_short(self) -> None:
        with pytest.raises(WampInvalidMessage):
            validate_cancel([WampMessageType.CANCEL, 1])

    def test_result_request_id_not_int(self) -> None:
        with pytest.raises(WampInvalidMessage):
            validate_result([WampMessageType.RESULT, "not_int", {}])

    def test_register_too_short(self) -> None:
        with pytest.raises(WampInvalidMessage):
            validate_register([WampMessageType.REGISTER, 1, {}])

    def test_invocation_too_short(self) -> None:
        with pytest.raises(WampInvalidMessage):
            validate_invocation([WampMessageType.INVOCATION, 1, 300])

    def test_yield_request_id_not_int(self) -> None:
        with pytest.raises(WampInvalidMessage):
            validate_yield([WampMessageType.YIELD, "not_int", {}])

    def test_interrupt_too_short(self) -> None:
        with pytest.raises(WampInvalidMessage):
            validate_interrupt([WampMessageType.INTERRUPT, 1])


# ---------------------------------------------------------------------------
# validate_message dispatch
# ---------------------------------------------------------------------------


class TestValidateMessageDispatch:
    """Test the top-level validate_message dispatcher."""

    def test_not_a_list(self) -> None:
        with pytest.raises(WampInvalidMessage, match="non-empty list"):
            validate_message("not a list")  # type: ignore[arg-type]

    def test_empty_list(self) -> None:
        with pytest.raises(WampInvalidMessage, match="non-empty list"):
            validate_message([])

    def test_unknown_type_code(self) -> None:
        with pytest.raises(WampInvalidMessage, match="Unknown WAMP message type code"):
            validate_message([999, "something"])

    def test_dispatches_to_hello_validator(self) -> None:
        # Valid hello -> no error
        validate_message([WampMessageType.HELLO, "realm1", {}])

    def test_dispatches_to_call_validator(self) -> None:
        # Valid call -> no error
        validate_message([WampMessageType.CALL, 1, {}, "com.example.add"])

    def test_dispatches_to_hello_validator_invalid(self) -> None:
        # Invalid hello -> error from hello validator
        with pytest.raises(WampInvalidMessage):
            validate_message([WampMessageType.HELLO, 123, {}])

    def test_dispatches_to_result_validator(self) -> None:
        validate_message([WampMessageType.RESULT, 1, {}, [42]])


# ---------------------------------------------------------------------------
# Message dataclass round-trip tests
# ---------------------------------------------------------------------------


class TestMessageDataclasses:
    """Test that message dataclasses produce correct WAMP arrays."""

    def test_hello_to_list(self) -> None:
        msg = HelloMessage(realm="realm1", details={"roles": {}})
        result = msg.to_list()
        assert result[0] == WampMessageType.HELLO
        assert result[1] == "realm1"
        assert result[2] == {"roles": {}}

    def test_welcome_to_list(self) -> None:
        msg = WelcomeMessage(session=12345, details={"roles": {}})
        result = msg.to_list()
        assert result[0] == WampMessageType.WELCOME
        assert result[1] == 12345

    def test_goodbye_to_list(self) -> None:
        msg = GoodbyeMessage(details={}, reason="wamp.error.close_realm")
        result = msg.to_list()
        assert result[0] == WampMessageType.GOODBYE
        assert result[2] == "wamp.error.close_realm"

    def test_call_to_list_minimal(self) -> None:
        msg = CallMessage(request_id=1, options={}, procedure="com.example.add")
        result = msg.to_list()
        assert result == [WampMessageType.CALL, 1, {}, "com.example.add"]

    def test_call_to_list_with_args(self) -> None:
        msg = CallMessage(
            request_id=1, options={}, procedure="com.example.add", arguments=[1, 2]
        )
        result = msg.to_list()
        assert result == [WampMessageType.CALL, 1, {}, "com.example.add", [1, 2]]

    def test_call_to_list_with_kwargs(self) -> None:
        msg = CallMessage(
            request_id=1,
            options={},
            procedure="com.example.add",
            arguments=[1],
            arguments_kw={"extra": True},
        )
        result = msg.to_list()
        assert result == [
            WampMessageType.CALL,
            1,
            {},
            "com.example.add",
            [1],
            {"extra": True},
        ]

    def test_result_to_list_minimal(self) -> None:
        msg = ResultMessage(request_id=1)
        result = msg.to_list()
        assert result == [WampMessageType.RESULT, 1, {}]

    def test_result_to_list_with_args(self) -> None:
        msg = ResultMessage(request_id=1, arguments=[42])
        result = msg.to_list()
        assert result == [WampMessageType.RESULT, 1, {}, [42]]

    def test_hello_validates_through_dispatch(self) -> None:
        msg = HelloMessage(realm="realm1", details={})
        validate_message(msg.to_list())

    def test_welcome_validates_through_dispatch(self) -> None:
        msg = WelcomeMessage(session=1, details={})
        validate_message(msg.to_list())
