"""WAMP message type aliases and data structures."""

from typing import Any

# WAMP message is a list of values
WampMessage = list[Any]

# WAMP URI string
WampUri = str

# WAMP ID (integer)
WampId = int

# WAMP args and kwargs
WampArgs = list[Any]
WampKwargs = dict[str, Any]
