"""WAMP session wrapping a FastAPI WebSocket."""


class WampSession:
    """Represents a single WAMP session over a WebSocket connection."""

    def __init__(self) -> None:
        self.session_id: int = 0
        self.is_open: bool = False
