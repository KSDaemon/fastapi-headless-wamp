"""WAMP message type aliases and data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi_headless_wamp.protocol import WampMessageType

# ---------------------------------------------------------------------------
# Primitive type aliases
# ---------------------------------------------------------------------------

# WAMP message is a list of values
WampMessage = list[Any]

# WAMP URI string
WampUri = str

# WAMP ID (integer)
WampId = int

# WAMP args and kwargs
WampArgs = list[Any]
WampKwargs = dict[str, Any]


# ---------------------------------------------------------------------------
# Typed default factories for pyright strict mode
# ---------------------------------------------------------------------------


def _dict_factory() -> dict[str, Any]:
    return {}


def _list_factory() -> list[Any]:
    return []


# ---------------------------------------------------------------------------
# Message dataclasses
# ---------------------------------------------------------------------------


@dataclass
class HelloMessage:
    """HELLO [1, Realm|uri, Details|dict]."""

    realm: str
    details: dict[str, Any] = field(default_factory=_dict_factory)

    def to_list(self) -> WampMessage:
        return [WampMessageType.HELLO, self.realm, self.details]


@dataclass
class WelcomeMessage:
    """WELCOME [2, Session|id, Details|dict]."""

    session: int
    details: dict[str, Any] = field(default_factory=_dict_factory)

    def to_list(self) -> WampMessage:
        return [WampMessageType.WELCOME, self.session, self.details]


@dataclass
class AbortMessage:
    """ABORT [3, Details|dict, Reason|uri]."""

    details: dict[str, Any]
    reason: str

    def to_list(self) -> WampMessage:
        return [WampMessageType.ABORT, self.details, self.reason]


@dataclass
class GoodbyeMessage:
    """GOODBYE [6, Details|dict, Reason|uri]."""

    details: dict[str, Any]
    reason: str

    def to_list(self) -> WampMessage:
        return [WampMessageType.GOODBYE, self.details, self.reason]


@dataclass
class ErrorMessage:
    """ERROR [8, REQUEST.Type|int, REQUEST.Request|id, Details|dict, Error|uri, Arguments|list?, ArgumentsKw|dict?]."""

    request_type: int
    request_id: int
    details: dict[str, Any]
    error: str
    arguments: WampArgs = field(default_factory=_list_factory)
    arguments_kw: WampKwargs = field(default_factory=_dict_factory)

    def to_list(self) -> WampMessage:
        msg: WampMessage = [
            WampMessageType.ERROR,
            self.request_type,
            self.request_id,
            self.details,
            self.error,
        ]
        if self.arguments_kw:
            msg.extend([self.arguments, self.arguments_kw])
        elif self.arguments:
            msg.append(self.arguments)
        return msg


@dataclass
class PublishMessage:
    """PUBLISH [16, Request|id, Options|dict, Topic|uri, Arguments|list?, ArgumentsKw|dict?]."""

    request_id: int
    options: dict[str, Any]
    topic: str
    arguments: WampArgs = field(default_factory=_list_factory)
    arguments_kw: WampKwargs = field(default_factory=_dict_factory)

    def to_list(self) -> WampMessage:
        msg: WampMessage = [
            WampMessageType.PUBLISH,
            self.request_id,
            self.options,
            self.topic,
        ]
        if self.arguments_kw:
            msg.extend([self.arguments, self.arguments_kw])
        elif self.arguments:
            msg.append(self.arguments)
        return msg


@dataclass
class PublishedMessage:
    """PUBLISHED [17, PUBLISH.Request|id, Publication|id]."""

    request_id: int
    publication_id: int

    def to_list(self) -> WampMessage:
        return [WampMessageType.PUBLISHED, self.request_id, self.publication_id]


@dataclass
class SubscribeMessage:
    """SUBSCRIBE [32, Request|id, Options|dict, Topic|uri]."""

    request_id: int
    options: dict[str, Any]
    topic: str

    def to_list(self) -> WampMessage:
        return [WampMessageType.SUBSCRIBE, self.request_id, self.options, self.topic]


@dataclass
class SubscribedMessage:
    """SUBSCRIBED [33, SUBSCRIBE.Request|id, Subscription|id]."""

    request_id: int
    subscription_id: int

    def to_list(self) -> WampMessage:
        return [WampMessageType.SUBSCRIBED, self.request_id, self.subscription_id]


@dataclass
class UnsubscribeMessage:
    """UNSUBSCRIBE [34, Request|id, SUBSCRIBED.Subscription|id]."""

    request_id: int
    subscription_id: int

    def to_list(self) -> WampMessage:
        return [WampMessageType.UNSUBSCRIBE, self.request_id, self.subscription_id]


@dataclass
class UnsubscribedMessage:
    """UNSUBSCRIBED [35, UNSUBSCRIBE.Request|id]."""

    request_id: int

    def to_list(self) -> WampMessage:
        return [WampMessageType.UNSUBSCRIBED, self.request_id]


@dataclass
class EventMessage:
    """EVENT [36, SUBSCRIBED.Subscription|id, PUBLISHED.Publication|id, Details|dict, Arguments|list?, ArgumentsKw|dict?]."""

    subscription_id: int
    publication_id: int
    details: dict[str, Any] = field(default_factory=_dict_factory)
    arguments: WampArgs = field(default_factory=_list_factory)
    arguments_kw: WampKwargs = field(default_factory=_dict_factory)

    def to_list(self) -> WampMessage:
        msg: WampMessage = [
            WampMessageType.EVENT,
            self.subscription_id,
            self.publication_id,
            self.details,
        ]
        if self.arguments_kw:
            msg.extend([self.arguments, self.arguments_kw])
        elif self.arguments:
            msg.append(self.arguments)
        return msg


@dataclass
class CallMessage:
    """CALL [48, Request|id, Options|dict, Procedure|uri, Arguments|list?, ArgumentsKw|dict?]."""

    request_id: int
    options: dict[str, Any]
    procedure: str
    arguments: WampArgs = field(default_factory=_list_factory)
    arguments_kw: WampKwargs = field(default_factory=_dict_factory)

    def to_list(self) -> WampMessage:
        msg: WampMessage = [
            WampMessageType.CALL,
            self.request_id,
            self.options,
            self.procedure,
        ]
        if self.arguments_kw:
            msg.extend([self.arguments, self.arguments_kw])
        elif self.arguments:
            msg.append(self.arguments)
        return msg


@dataclass
class CancelMessage:
    """CANCEL [49, CALL.Request|id, Options|dict]."""

    request_id: int
    options: dict[str, Any] = field(default_factory=_dict_factory)

    def to_list(self) -> WampMessage:
        return [WampMessageType.CANCEL, self.request_id, self.options]


@dataclass
class ResultMessage:
    """RESULT [50, CALL.Request|id, Details|dict, YIELD.Arguments|list?, YIELD.ArgumentsKw|dict?]."""

    request_id: int
    details: dict[str, Any] = field(default_factory=_dict_factory)
    arguments: WampArgs = field(default_factory=_list_factory)
    arguments_kw: WampKwargs = field(default_factory=_dict_factory)

    def to_list(self) -> WampMessage:
        msg: WampMessage = [WampMessageType.RESULT, self.request_id, self.details]
        if self.arguments_kw:
            msg.extend([self.arguments, self.arguments_kw])
        elif self.arguments:
            msg.append(self.arguments)
        return msg


@dataclass
class RegisterMessage:
    """REGISTER [64, Request|id, Options|dict, Procedure|uri]."""

    request_id: int
    options: dict[str, Any]
    procedure: str

    def to_list(self) -> WampMessage:
        return [WampMessageType.REGISTER, self.request_id, self.options, self.procedure]


@dataclass
class RegisteredMessage:
    """REGISTERED [65, REGISTER.Request|id, Registration|id]."""

    request_id: int
    registration_id: int

    def to_list(self) -> WampMessage:
        return [WampMessageType.REGISTERED, self.request_id, self.registration_id]


@dataclass
class UnregisterMessage:
    """UNREGISTER [66, Request|id, REGISTERED.Registration|id]."""

    request_id: int
    registration_id: int

    def to_list(self) -> WampMessage:
        return [WampMessageType.UNREGISTER, self.request_id, self.registration_id]


@dataclass
class UnregisteredMessage:
    """UNREGISTERED [67, UNREGISTER.Request|id]."""

    request_id: int

    def to_list(self) -> WampMessage:
        return [WampMessageType.UNREGISTERED, self.request_id]


@dataclass
class InvocationMessage:
    """INVOCATION [68, Request|id, REGISTERED.Registration|id, Details|dict, Arguments|list?, ArgumentsKw|dict?]."""

    request_id: int
    registration_id: int
    details: dict[str, Any] = field(default_factory=_dict_factory)
    arguments: WampArgs = field(default_factory=_list_factory)
    arguments_kw: WampKwargs = field(default_factory=_dict_factory)

    def to_list(self) -> WampMessage:
        msg: WampMessage = [
            WampMessageType.INVOCATION,
            self.request_id,
            self.registration_id,
            self.details,
        ]
        if self.arguments_kw:
            msg.extend([self.arguments, self.arguments_kw])
        elif self.arguments:
            msg.append(self.arguments)
        return msg


@dataclass
class InterruptMessage:
    """INTERRUPT [69, INVOCATION.Request|id, Options|dict]."""

    request_id: int
    options: dict[str, Any] = field(default_factory=_dict_factory)

    def to_list(self) -> WampMessage:
        return [WampMessageType.INTERRUPT, self.request_id, self.options]


@dataclass
class YieldMessage:
    """YIELD [70, INVOCATION.Request|id, Options|dict, Arguments|list?, ArgumentsKw|dict?]."""

    request_id: int
    options: dict[str, Any] = field(default_factory=_dict_factory)
    arguments: WampArgs = field(default_factory=_list_factory)
    arguments_kw: WampKwargs = field(default_factory=_dict_factory)

    def to_list(self) -> WampMessage:
        msg: WampMessage = [WampMessageType.YIELD, self.request_id, self.options]
        if self.arguments_kw:
            msg.extend([self.arguments, self.arguments_kw])
        elif self.arguments:
            msg.append(self.arguments)
        return msg
