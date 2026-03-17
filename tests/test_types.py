"""Tests for WAMP message dataclasses and their to_list() serialization."""

from __future__ import annotations

from fastapi_headless_wamp.protocol import WampMessageType
from fastapi_headless_wamp.types import (
    AbortMessage,
    CallMessage,
    CancelMessage,
    ErrorMessage,
    EventMessage,
    GoodbyeMessage,
    HelloMessage,
    InterruptMessage,
    InvocationMessage,
    PublishedMessage,
    PublishMessage,
    RegisteredMessage,
    RegisterMessage,
    ResultMessage,
    SubscribedMessage,
    SubscribeMessage,
    UnregisteredMessage,
    UnregisterMessage,
    UnsubscribedMessage,
    UnsubscribeMessage,
    WelcomeMessage,
    YieldMessage,
)


class TestHelloMessage:
    def test_to_list(self) -> None:
        msg = HelloMessage(realm="realm1", details={"roles": {}})
        result = msg.to_list()
        assert result == [WampMessageType.HELLO, "realm1", {"roles": {}}]

    def test_to_list_default_details(self) -> None:
        msg = HelloMessage(realm="realm1")
        result = msg.to_list()
        assert result == [WampMessageType.HELLO, "realm1", {}]


class TestWelcomeMessage:
    def test_to_list(self) -> None:
        msg = WelcomeMessage(session=12345, details={"roles": {}})
        result = msg.to_list()
        assert result == [WampMessageType.WELCOME, 12345, {"roles": {}}]


class TestAbortMessage:
    def test_to_list(self) -> None:
        msg = AbortMessage(details={"message": "no realm"}, reason="wamp.error.no_such_realm")
        result = msg.to_list()
        assert result == [WampMessageType.ABORT, {"message": "no realm"}, "wamp.error.no_such_realm"]


class TestGoodbyeMessage:
    def test_to_list(self) -> None:
        msg = GoodbyeMessage(details={}, reason="wamp.close.normal")
        result = msg.to_list()
        assert result == [WampMessageType.GOODBYE, {}, "wamp.close.normal"]


class TestErrorMessage:
    def test_to_list_minimal(self) -> None:
        msg = ErrorMessage(request_type=48, request_id=1, details={}, error="wamp.error.runtime_error")
        result = msg.to_list()
        assert result == [WampMessageType.ERROR, 48, 1, {}, "wamp.error.runtime_error"]

    def test_to_list_with_args(self) -> None:
        msg = ErrorMessage(
            request_type=48,
            request_id=1,
            details={},
            error="wamp.error.runtime_error",
            arguments=["some error"],
        )
        result = msg.to_list()
        assert result == [WampMessageType.ERROR, 48, 1, {}, "wamp.error.runtime_error", ["some error"]]

    def test_to_list_with_kwargs(self) -> None:
        msg = ErrorMessage(
            request_type=48,
            request_id=1,
            details={},
            error="wamp.error.runtime_error",
            arguments=["err"],
            arguments_kw={"detail": "info"},
        )
        result = msg.to_list()
        assert result == [
            WampMessageType.ERROR,
            48,
            1,
            {},
            "wamp.error.runtime_error",
            ["err"],
            {"detail": "info"},
        ]

    def test_to_list_with_only_kwargs(self) -> None:
        """kwargs present but args empty — args still included as placeholder."""
        msg = ErrorMessage(
            request_type=48,
            request_id=1,
            details={},
            error="wamp.error.runtime_error",
            arguments_kw={"detail": "info"},
        )
        result = msg.to_list()
        assert result == [WampMessageType.ERROR, 48, 1, {}, "wamp.error.runtime_error", [], {"detail": "info"}]


class TestPublishMessage:
    def test_to_list_minimal(self) -> None:
        msg = PublishMessage(request_id=1, options={}, topic="com.example.topic")
        result = msg.to_list()
        assert result == [WampMessageType.PUBLISH, 1, {}, "com.example.topic"]

    def test_to_list_with_args(self) -> None:
        msg = PublishMessage(request_id=1, options={}, topic="com.example.topic", arguments=["hello"])
        result = msg.to_list()
        assert result == [WampMessageType.PUBLISH, 1, {}, "com.example.topic", ["hello"]]

    def test_to_list_with_kwargs(self) -> None:
        msg = PublishMessage(
            request_id=1,
            options={},
            topic="com.example.topic",
            arguments=["hello"],
            arguments_kw={"key": "val"},
        )
        result = msg.to_list()
        assert result == [WampMessageType.PUBLISH, 1, {}, "com.example.topic", ["hello"], {"key": "val"}]


class TestPublishedMessage:
    def test_to_list(self) -> None:
        msg = PublishedMessage(request_id=1, publication_id=42)
        result = msg.to_list()
        assert result == [WampMessageType.PUBLISHED, 1, 42]


class TestSubscribeMessage:
    def test_to_list(self) -> None:
        msg = SubscribeMessage(request_id=1, options={}, topic="com.example.topic")
        result = msg.to_list()
        assert result == [WampMessageType.SUBSCRIBE, 1, {}, "com.example.topic"]


class TestSubscribedMessage:
    def test_to_list(self) -> None:
        msg = SubscribedMessage(request_id=1, subscription_id=10)
        result = msg.to_list()
        assert result == [WampMessageType.SUBSCRIBED, 1, 10]


class TestUnsubscribeMessage:
    def test_to_list(self) -> None:
        msg = UnsubscribeMessage(request_id=2, subscription_id=10)
        result = msg.to_list()
        assert result == [WampMessageType.UNSUBSCRIBE, 2, 10]


class TestUnsubscribedMessage:
    def test_to_list(self) -> None:
        msg = UnsubscribedMessage(request_id=2)
        result = msg.to_list()
        assert result == [WampMessageType.UNSUBSCRIBED, 2]


class TestEventMessage:
    def test_to_list_minimal(self) -> None:
        msg = EventMessage(subscription_id=5, publication_id=42)
        result = msg.to_list()
        assert result == [WampMessageType.EVENT, 5, 42, {}]

    def test_to_list_with_args(self) -> None:
        msg = EventMessage(subscription_id=5, publication_id=42, arguments=["data"])
        result = msg.to_list()
        assert result == [WampMessageType.EVENT, 5, 42, {}, ["data"]]

    def test_to_list_with_kwargs(self) -> None:
        msg = EventMessage(
            subscription_id=5,
            publication_id=42,
            arguments=["data"],
            arguments_kw={"key": "val"},
        )
        result = msg.to_list()
        assert result == [WampMessageType.EVENT, 5, 42, {}, ["data"], {"key": "val"}]


class TestCallMessage:
    def test_to_list_minimal(self) -> None:
        msg = CallMessage(request_id=1, options={}, procedure="com.example.add")
        result = msg.to_list()
        assert result == [WampMessageType.CALL, 1, {}, "com.example.add"]

    def test_to_list_with_args(self) -> None:
        msg = CallMessage(request_id=1, options={}, procedure="com.example.add", arguments=[1, 2])
        result = msg.to_list()
        assert result == [WampMessageType.CALL, 1, {}, "com.example.add", [1, 2]]

    def test_to_list_with_kwargs(self) -> None:
        msg = CallMessage(
            request_id=1,
            options={},
            procedure="com.example.add",
            arguments=[1, 2],
            arguments_kw={"extra": True},
        )
        result = msg.to_list()
        assert result == [WampMessageType.CALL, 1, {}, "com.example.add", [1, 2], {"extra": True}]


class TestCancelMessage:
    def test_to_list(self) -> None:
        msg = CancelMessage(request_id=1, options={"mode": "kill"})
        result = msg.to_list()
        assert result == [WampMessageType.CANCEL, 1, {"mode": "kill"}]

    def test_to_list_default_options(self) -> None:
        msg = CancelMessage(request_id=1)
        result = msg.to_list()
        assert result == [WampMessageType.CANCEL, 1, {}]


class TestResultMessage:
    def test_to_list_minimal(self) -> None:
        msg = ResultMessage(request_id=1)
        result = msg.to_list()
        assert result == [WampMessageType.RESULT, 1, {}]

    def test_to_list_with_args(self) -> None:
        msg = ResultMessage(request_id=1, arguments=[42])
        result = msg.to_list()
        assert result == [WampMessageType.RESULT, 1, {}, [42]]

    def test_to_list_with_kwargs(self) -> None:
        msg = ResultMessage(request_id=1, arguments=[42], arguments_kw={"extra": True})
        result = msg.to_list()
        assert result == [WampMessageType.RESULT, 1, {}, [42], {"extra": True}]


class TestRegisterMessage:
    def test_to_list(self) -> None:
        msg = RegisterMessage(request_id=1, options={}, procedure="com.example.add")
        result = msg.to_list()
        assert result == [WampMessageType.REGISTER, 1, {}, "com.example.add"]


class TestRegisteredMessage:
    def test_to_list(self) -> None:
        msg = RegisteredMessage(request_id=1, registration_id=100)
        result = msg.to_list()
        assert result == [WampMessageType.REGISTERED, 1, 100]


class TestUnregisterMessage:
    def test_to_list(self) -> None:
        msg = UnregisterMessage(request_id=2, registration_id=100)
        result = msg.to_list()
        assert result == [WampMessageType.UNREGISTER, 2, 100]


class TestUnregisteredMessage:
    def test_to_list(self) -> None:
        msg = UnregisteredMessage(request_id=2)
        result = msg.to_list()
        assert result == [WampMessageType.UNREGISTERED, 2]


class TestInvocationMessage:
    def test_to_list_minimal(self) -> None:
        msg = InvocationMessage(request_id=1, registration_id=100)
        result = msg.to_list()
        assert result == [WampMessageType.INVOCATION, 1, 100, {}]

    def test_to_list_with_args(self) -> None:
        msg = InvocationMessage(request_id=1, registration_id=100, arguments=[1, 2])
        result = msg.to_list()
        assert result == [WampMessageType.INVOCATION, 1, 100, {}, [1, 2]]

    def test_to_list_with_kwargs(self) -> None:
        msg = InvocationMessage(
            request_id=1,
            registration_id=100,
            arguments=[1, 2],
            arguments_kw={"extra": True},
        )
        result = msg.to_list()
        assert result == [WampMessageType.INVOCATION, 1, 100, {}, [1, 2], {"extra": True}]


class TestInterruptMessage:
    def test_to_list(self) -> None:
        msg = InterruptMessage(request_id=1, options={"mode": "kill"})
        result = msg.to_list()
        assert result == [WampMessageType.INTERRUPT, 1, {"mode": "kill"}]

    def test_to_list_default_options(self) -> None:
        msg = InterruptMessage(request_id=1)
        result = msg.to_list()
        assert result == [WampMessageType.INTERRUPT, 1, {}]


class TestYieldMessage:
    def test_to_list_minimal(self) -> None:
        msg = YieldMessage(request_id=1)
        result = msg.to_list()
        assert result == [WampMessageType.YIELD, 1, {}]

    def test_to_list_with_args(self) -> None:
        msg = YieldMessage(request_id=1, arguments=[42])
        result = msg.to_list()
        assert result == [WampMessageType.YIELD, 1, {}, [42]]

    def test_to_list_with_kwargs(self) -> None:
        msg = YieldMessage(request_id=1, arguments=[42], arguments_kw={"extra": True})
        result = msg.to_list()
        assert result == [WampMessageType.YIELD, 1, {}, [42], {"extra": True}]
