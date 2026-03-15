"""Bidirectional RPC example: server calling client-registered procedures.

Run with:
    uvicorn examples.bidirectional:app --host 0.0.0.0 --port 8000

Demonstrates:
- Server-side RPC handlers that clients can call.
- Server calling RPCs that clients have registered on their session.
- Session lifecycle callbacks for interacting with connected clients.

The client connects, registers its own RPCs (e.g. ``com.client.get_info``),
and the server calls them when the session opens.
"""

import asyncio

from fastapi import FastAPI
from fastapi_headless_wamp import WampHub, WampSession

app = FastAPI(title="fastapi-headless-wamp Bidirectional RPC Example")
wamp = WampHub(realm="realm1")


# --- Server-side RPCs (clients call these) ---


@wamp.register("com.server.ping")
async def ping() -> str:
    """Simple ping from client to server."""
    return "pong"


@wamp.register("com.server.time")
async def server_time() -> str:
    """Return the current server time."""
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# --- Lifecycle callback: call client RPCs when session opens ---


@wamp.on_session_open
async def on_session_open(session: WampSession) -> None:
    """Called when a client completes the WAMP handshake.

    We schedule a task to call client-registered RPCs after a short
    delay, giving the client time to register its procedures.
    """

    async def interact_with_client() -> None:
        # Give the client a moment to register its procedures
        await asyncio.sleep(0.5)

        try:
            # Call a client-registered RPC
            info = await session.call("com.client.get_info", timeout=5.0)
            print(f"Client info: {info}")
        except Exception as exc:
            print(f"Could not call client RPC: {exc}")

        try:
            # Call another client RPC with arguments
            result = await session.call(
                "com.client.compute",
                args=[10, 20],
                timeout=5.0,
            )
            print(f"Client compute result: {result}")
        except Exception as exc:
            print(f"Could not call client compute: {exc}")

    asyncio.create_task(interact_with_client())


@wamp.on_session_close
async def on_session_close(session: WampSession) -> None:
    """Called when a client disconnects."""
    print(f"Session {session.session_id} disconnected")


# Mount the WAMP WebSocket endpoint
app.include_router(wamp.get_router(path="/ws"))
