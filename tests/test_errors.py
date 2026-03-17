"""Tests for WAMP error hierarchy."""

import pytest

from fastapi_headless_wamp.errors import (
    WampCallTimeout,
    WampCanceled,
    WampError,
    WampInvalidMessage,
    WampNoSuchProcedure,
    WampNoSuchSubscription,
    WampProcedureAlreadyExists,
    WampProtocolError,
    WampRuntimeError,
)

# ---------------------------------------------------------------------------
# Inheritance hierarchy
# ---------------------------------------------------------------------------


class TestErrorHierarchy:
    """Verify all WAMP errors descend from WampError (and Exception)."""

    @pytest.mark.parametrize(
        "cls",
        [
            WampError,
            WampProtocolError,
            WampInvalidMessage,
            WampNoSuchProcedure,
            WampNoSuchSubscription,
            WampRuntimeError,
            WampCallTimeout,
            WampCanceled,
            WampProcedureAlreadyExists,
        ],
    )
    def test_is_exception(self, cls: type[WampError]) -> None:
        assert issubclass(cls, Exception)

    @pytest.mark.parametrize(
        "cls",
        [
            WampProtocolError,
            WampInvalidMessage,
            WampNoSuchProcedure,
            WampNoSuchSubscription,
            WampRuntimeError,
            WampCallTimeout,
            WampCanceled,
            WampProcedureAlreadyExists,
        ],
    )
    def test_subclass_of_wamp_error(self, cls: type[WampError]) -> None:
        assert issubclass(cls, WampError)

    def test_catch_via_base_class(self) -> None:
        """Raising a subclass should be catchable via WampError."""
        with pytest.raises(WampError):
            raise WampNoSuchProcedure("test.procedure")


# ---------------------------------------------------------------------------
# WAMP error URI attributes
# ---------------------------------------------------------------------------


class TestErrorURIs:
    """Each error class carries the correct WAMP error URI."""

    def test_wamp_error_uri(self) -> None:
        assert WampError.uri == "wamp.error"

    def test_protocol_error_uri(self) -> None:
        assert WampProtocolError.uri == "wamp.error.protocol_error"

    def test_invalid_message_uri(self) -> None:
        assert WampInvalidMessage.uri == "wamp.error.invalid_message"

    def test_no_such_procedure_uri(self) -> None:
        assert WampNoSuchProcedure.uri == "wamp.error.no_such_procedure"

    def test_no_such_subscription_uri(self) -> None:
        assert WampNoSuchSubscription.uri == "wamp.error.no_such_subscription"

    def test_runtime_error_uri(self) -> None:
        assert WampRuntimeError.uri == "wamp.error.runtime_error"

    def test_call_timeout_uri(self) -> None:
        assert WampCallTimeout.uri == "wamp.error.canceled"

    def test_canceled_uri(self) -> None:
        assert WampCanceled.uri == "wamp.error.canceled"

    def test_procedure_already_exists_uri(self) -> None:
        assert WampProcedureAlreadyExists.uri == "wamp.error.procedure_already_exists"

    def test_uri_accessible_on_instance(self) -> None:
        """URI should also be accessible on instances, not just the class."""
        err = WampNoSuchProcedure("com.example.missing")
        assert err.uri == "wamp.error.no_such_procedure"


# ---------------------------------------------------------------------------
# Message formatting (str representation)
# ---------------------------------------------------------------------------


class TestMessageFormatting:
    """Errors have useful string representations."""

    def test_default_message_empty(self) -> None:
        err = WampError()
        assert str(err) == ""

    def test_custom_message(self) -> None:
        err = WampError("something went wrong")
        assert str(err) == "something went wrong"

    def test_subclass_message(self) -> None:
        err = WampNoSuchProcedure("com.example.nope")
        assert str(err) == "com.example.nope"

    def test_repr_contains_class_name(self) -> None:
        err = WampRuntimeError("boom")
        assert "WampRuntimeError" in repr(err)


# ---------------------------------------------------------------------------
# WAMP ERROR payload: args and kwargs
# ---------------------------------------------------------------------------


class TestPayload:
    """Errors can carry args and kwargs for the WAMP ERROR message payload."""

    def test_default_args_and_kwargs(self) -> None:
        err = WampError("test")
        assert err.wamp_args == []
        assert err.wamp_kwargs == {}

    def test_custom_args(self) -> None:
        err = WampError("test", args=["value1", 42])
        assert err.wamp_args == ["value1", 42]

    def test_custom_kwargs(self) -> None:
        err = WampError("test", kwargs={"key": "val"})
        assert err.wamp_kwargs == {"key": "val"}

    def test_both_args_and_kwargs(self) -> None:
        err = WampError("test", args=[1, 2], kwargs={"a": "b"})
        assert err.wamp_args == [1, 2]
        assert err.wamp_kwargs == {"a": "b"}

    def test_subclass_inherits_payload(self) -> None:
        err = WampRuntimeError(
            "handler failed", args=["trace info"], kwargs={"code": 500}
        )
        assert err.wamp_args == ["trace info"]
        assert err.wamp_kwargs == {"code": 500}

    def test_none_args_becomes_empty_list(self) -> None:
        err = WampError("test", args=None)
        assert err.wamp_args == []

    def test_none_kwargs_becomes_empty_dict(self) -> None:
        err = WampError("test", kwargs=None)
        assert err.wamp_kwargs == {}


# ---------------------------------------------------------------------------
# Raising and catching with payload
# ---------------------------------------------------------------------------


class TestRaiseAndCatch:
    """Verify that payload survives raise/except round-trips."""

    def test_catch_preserves_args(self) -> None:
        try:
            raise WampRuntimeError("fail", args=["detail"], kwargs={"x": 1})
        except WampError as e:
            assert e.wamp_args == ["detail"]
            assert e.wamp_kwargs == {"x": 1}
            assert e.uri == "wamp.error.runtime_error"

    def test_catch_no_such_procedure(self) -> None:
        try:
            raise WampNoSuchProcedure("com.example.missing")
        except WampNoSuchProcedure as e:
            assert str(e) == "com.example.missing"
            assert e.uri == "wamp.error.no_such_procedure"

    def test_catch_timeout(self) -> None:
        try:
            raise WampCallTimeout("timed out after 5s")
        except WampCallTimeout as e:
            assert str(e) == "timed out after 5s"
            assert e.uri == "wamp.error.canceled"

    def test_catch_canceled(self) -> None:
        try:
            raise WampCanceled("client canceled")
        except WampCanceled as e:
            assert str(e) == "client canceled"
            assert e.uri == "wamp.error.canceled"

    def test_catch_procedure_already_exists(self) -> None:
        try:
            raise WampProcedureAlreadyExists("com.example.dup")
        except WampProcedureAlreadyExists as e:
            assert str(e) == "com.example.dup"
            assert e.uri == "wamp.error.procedure_already_exists"
