"""End-to-end demo server: RPC + PubSub with fastapi-headless-wamp.

Run with:
    uvicorn examples.e2e.server:app --host 0.0.0.0 --port 8080

This server exposes several WAMP features for the companion client
script (client.mjs) to exercise:

1. RPC procedures the client can call.
2. PubSub topics the server listens on — when the client publishes,
   the server reacts and re-publishes a transformed event to all
   connected sessions.
3. A session-open hook that publishes a welcome event to the newly
   connected client.
"""

import asyncio
import datetime

from fastapi import FastAPI

from fastapi_headless_wamp import WampHub, WampService, WampSession, rpc, subscribe

app = FastAPI(title="fastapi-headless-wamp E2E Demo")
wamp = WampHub(realm="realm1")


# ---------------------------------------------------------------------------
# 1. Standalone RPC handlers
# ---------------------------------------------------------------------------


@wamp.register("com.example.add")
async def add(a: int, b: int) -> int:
    """Add two numbers and return the result."""
    print(f"  [server] com.example.add({a}, {b}) -> {a + b}")
    return a + b


@wamp.register("com.example.greet")
async def greet(name: str) -> str:
    """Return a personalised greeting."""
    result = f"Hello, {name}!"
    print(f"  [server] com.example.greet({name!r}) -> {result!r}")
    return result


@wamp.register("com.example.time")
async def server_time() -> str:
    """Return the current UTC time as ISO-8601 string."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    print(f"  [server] com.example.time() -> {now!r}")
    return now


# ---------------------------------------------------------------------------
# 2. Class-based service with RPC + subscription
# ---------------------------------------------------------------------------


class EchoService(WampService):
    """A small service that echoes messages and counts calls."""

    prefix = "com.example.echo"

    def __init__(self) -> None:
        super().__init__()
        self._call_count = 0

    @rpc("reverse")
    async def reverse(self, text: str) -> str:
        """Return the reversed text."""
        self._call_count += 1
        result = text[::-1]
        print(f"  [server] com.example.echo.reverse({text!r}) -> {result!r}")
        return result

    @rpc("stats")
    async def stats(self) -> dict[str, int]:
        """Return call statistics."""
        return {"total_calls": self._call_count}

    @subscribe("shout")
    async def on_shout(
        self,
        text: str,
        _session: WampSession | None = None,
    ) -> None:
        """When a client publishes to ``com.example.echo.shout``,
        the server uppercases the text and re-publishes it to
        ``com.example.echo.loud`` for all connected sessions.
        """
        sender = _session.session_id if _session else "unknown"
        loud = text.upper()
        print(
            f"  [server] com.example.echo.shout from {sender}: {text!r} -> broadcasting {loud!r}"
        )

        if self.hub is not None:
            await self.hub.publish_to_all(
                "com.example.echo.loud",
                args=[loud],
                kwargs={"original_sender": sender},
            )


wamp.register_service(EchoService())


# ---------------------------------------------------------------------------
# 3. Server subscribes to a topic (standalone handler)
# ---------------------------------------------------------------------------


@wamp.subscribe("com.example.ping")
async def on_ping(
    message: str = "ping",
    _session: WampSession | None = None,
) -> None:
    """React to client pings by publishing a pong to all sessions."""
    sender = _session.session_id if _session else "unknown"
    print(f"  [server] com.example.ping from {sender}: {message!r}")

    await wamp.publish_to_all(
        "com.example.pong",
        args=[f"pong:{message}"],
        kwargs={"from_session": sender},
    )


# ---------------------------------------------------------------------------
# 4. Session lifecycle
# ---------------------------------------------------------------------------


@wamp.on_session_open
async def welcome_client(session: WampSession) -> None:
    """Publish a welcome event when a new client connects.

    We schedule the publish as a background task because
    ``on_session_open`` runs *before* the message loop starts.
    If we ``await asyncio.sleep()`` here we would block the loop
    and the client's SUBSCRIBE messages would not be processed yet.
    """
    print(f"  [server] Session {session.session_id} connected — scheduling welcome")

    async def _send_welcome() -> None:
        # Give the client time to set up its subscriptions
        await asyncio.sleep(0.5)
        await session.publish(
            "com.example.welcome",
            args=[f"Welcome! Your session ID is {session.session_id}."],
        )
        print(f"  [server] Sent welcome event to session {session.session_id}")

    asyncio.create_task(_send_welcome())


@wamp.on_session_close
async def farewell_client(session: WampSession) -> None:
    """Notify remaining clients when someone disconnects."""
    print(f"  [server] Session {session.session_id} disconnected")
    await wamp.publish_to_all(
        "com.example.client_left",
        args=[session.session_id],
    )


# ---------------------------------------------------------------------------
# Mount the WAMP WebSocket endpoint
# ---------------------------------------------------------------------------

app.include_router(wamp.get_router(path="/ws"))
