"""PubSub example: subscribe and publish in both directions.

Run with:
    uvicorn examples.pubsub:app --host 0.0.0.0 --port 8000

Demonstrates:
- Server subscribing to topics and handling client-published events.
- Server publishing events to subscribed clients.
- Class-based subscription handlers via WampService.
- Broadcasting events to all connected sessions.
"""

import asyncio
from typing import Any

from fastapi import FastAPI

from fastapi_headless_wamp import WampHub, WampService, WampSession, rpc, subscribe

app = FastAPI(title="fastapi-headless-wamp PubSub Example")
wamp = WampHub(realm="realm1")


# --- Server subscribes to client-published events (standalone) ---


@wamp.subscribe("com.chat.message")
async def on_chat_message(
    session: WampSession,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Handle chat messages published by clients.

    The ``session`` parameter is always passed as the first positional
    argument, providing access to the publishing client's session.
    """
    text = args[0] if args else kwargs.get("text", "")
    sender = session.session_id
    print(f"[Chat] Session {sender}: {text}")

    # Re-broadcast the message to all other connected clients
    await wamp.publish_to_all(
        "com.chat.message",
        args=[text],
        kwargs={"sender": sender},
    )


@wamp.subscribe("com.telemetry.report")
async def on_telemetry(
    session: WampSession,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Handle telemetry data published by clients."""
    print(f"[Telemetry] Received from {session.session_id}: {kwargs}")


# --- Class-based subscription handler ---


class NotificationService(WampService):
    """Handles notification-related events and RPCs."""

    prefix = "com.notifications"

    @subscribe("new")
    async def on_new_notification(
        self,
        session: WampSession,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Handle new notification events from clients.

        Full topic: ``com.notifications.new``
        """
        title = kwargs.get("title", args[0] if len(args) > 0 else "")
        body = kwargs.get("body", args[1] if len(args) > 1 else "")
        sender = session.session_id
        print(f"[Notification] From session {sender}: {title} - {body}")

    @rpc()
    async def send_to_all(self, session: WampSession, *, title: str, body: str) -> int:
        """RPC to broadcast a notification to all connected clients.

        Returns the number of active sessions.
        """
        if self.hub is not None:
            await self.hub.publish_to_all(
                "com.notifications.alert",
                args=[title, body],
            )
            return self.hub.session_count
        return 0


wamp.register_service(NotificationService())


# --- Server publishes events to clients ---


@wamp.on_session_open
async def on_session_open(session: WampSession) -> None:
    """When a client connects, send a welcome event and start a ticker."""
    # Publish a welcome event to this specific session
    await session.publish(
        "com.server.welcome",
        args=[f"Welcome, session {session.session_id}!"],
    )

    # Start a periodic ticker that publishes to this session
    async def ticker() -> None:
        count = 0
        while session.is_open:
            count += 1
            await session.publish(
                "com.server.tick",
                args=[count],
                kwargs={"session_id": session.session_id},
            )
            await asyncio.sleep(5.0)

    asyncio.create_task(ticker())


@wamp.on_session_close
async def on_session_close(session: WampSession) -> None:
    """Notify other sessions when someone disconnects."""
    await wamp.publish_to_all(
        "com.server.user_left",
        args=[session.session_id],
    )


# Mount the WAMP WebSocket endpoint
app.include_router(wamp.get_router(path="/ws"))
