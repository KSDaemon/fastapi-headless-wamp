# fastapi-headless-wamp

Full bidirectional [WAMP](https://wamp-proto.org/) (Web Application Messaging Protocol) RPC and PubSub 
for [FastAPI](https://fastapi.tiangolo.com/) — **without a separate WAMP router**.

Each WebSocket connection gets its own isolated peer-to-peer session between the server and the client. 
There is no shared broker or dealer infrastructure. This makes the library lightweight, easy to deploy, and ideal 
for applications that need structured bidirectional communication over WebSockets.

## Features

- **Bidirectional RPC** — register server-side procedures that clients can call, *and* call client-registered 
  procedures from the server.
- **PubSub** — subscribe to topics and publish events in both directions (server-to-client and client-to-server).
- **Progressive results** — stream intermediate results from long-running RPCs.
- **Progressive call invocations** — clients can stream input chunks to the server.
- **Call cancellation** — cancel in-flight RPCs from either side.
- **Class-based services** — group related RPCs and subscriptions into service classes with a URI prefix.
- **Pluggable serialization** — JSON by default, extensible to other formats.
- **Typed** — fully typed with strict linting via ruff.
- **wampy.js compatible** — designed to work with [wampy.js](https://github.com/nicola/wampy.js) and other WAMP clients.

## Architecture

```
┌──────────────┐  WebSocket  ┌───────────────────────┐
│  WAMP Client │◄───────────►│  FastAPI Application  │
│  (wampy.js)  │  wamp.2.json│                       │
└──────────────┘             │  WampHub              │
                             │  ├─ session 1 (P2P)   │
┌──────────────┐  WebSocket  │  ├─ session 2 (P2P)   │
│  WAMP Client │◄───────────►│  └─ session N (P2P)   │
└──────────────┘             └───────────────────────┘
```

Each WebSocket connection creates an **isolated peer-to-peer WAMP session**. There is no shared router — the server 
and client communicate directly within the session. This means:

- **No external WAMP router** (like Crossbar.io) is required.
- Each session is fully independent: client-registered RPCs and subscriptions are scoped to that session.
- Server-side RPCs and subscriptions (registered via decorators) are shared across all sessions.

## Installation

```bash
pip install fastapi-headless-wamp
```

## Quick Start

### Standalone Decorators

```python
from fastapi import FastAPI
from fastapi_headless_wamp import WampHub

app = FastAPI()
wamp = WampHub(realm="realm1")

# Register a server-side RPC
@wamp.register("com.example.add")
async def add(a: int, b: int) -> int:
    return a + b

# Mount the WAMP WebSocket endpoint
app.include_router(wamp.get_router(path="/ws"))
```

Run with `uvicorn`:

```bash
uvicorn myapp:app --host 0.0.0.0 --port 8000
```

Clients connect via WebSocket to `ws://localhost:8000/ws` using the `wamp.2.json` subprotocol and 
can call `com.example.add`.

### Class-Based Services

```python
from fastapi import FastAPI
from fastapi_headless_wamp import WampHub, WampService, rpc

app = FastAPI()
wamp = WampHub(realm="realm1")

class MathService(WampService):
    prefix = "com.example.math"

    @rpc()
    async def add(self, a: int, b: int) -> int:
        return a + b

    @rpc("multiply")
    async def mul(self, a: int, b: int) -> int:
        return a * b

wamp.register_service(MathService())
app.include_router(wamp.get_router(path="/ws"))
```

Clients can now call `com.example.math.add` and `com.example.math.multiply`.

### Manual WebSocket Handler

If you need more control, use `handle_websocket` directly:

```python
from fastapi import FastAPI, WebSocket
from fastapi_headless_wamp import WampHub

app = FastAPI()
wamp = WampHub(realm="realm1")

@wamp.register("com.example.greet")
async def greet(name: str) -> str:
    return f"Hello, {name}!"

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await wamp.handle_websocket(websocket)
```

## Bidirectional RPC

The server can call RPCs that clients have registered on their session:

```python
from fastapi_headless_wamp import WampHub, WampSession

wamp = WampHub(realm="realm1")

@wamp.on_session_open
async def on_open(session: WampSession) -> None:
    # Call a client-registered procedure
    result = await session.call("com.client.get_status")
    print(f"Client status: {result}")
```

## PubSub

### Server publishes to clients

```python
@wamp.on_session_open
async def on_open(session: WampSession) -> None:
    # Publish to this specific session (if subscribed)
    await session.publish("com.example.news", args=["breaking update"])

    # Publish to all connected sessions that are subscribed
    await wamp.publish_to_all("com.example.news", args=["broadcast"])
```

### Server handles client publishes

```python
@wamp.subscribe("com.example.chat")
async def on_chat(message: str, _session: WampSession | None = None) -> None:
    print(f"Chat from session {_session.session_id if _session else '?'}: {message}")
```

## Progressive Results

Server RPC handlers can stream intermediate results:

```python
from typing import Any
from collections.abc import Awaitable, Callable

@wamp.register("com.example.long_task")
async def long_task(
    n: int,
    _progress: Callable[[Any], Awaitable[None]] | None = None,
) -> str:
    for i in range(n):
        if _progress is not None:
            await _progress(f"Step {i + 1}/{n}")
    return "done"
```

The client receives each progressive result as it is produced, then the final result.

## Call Cancellation

Clients can cancel in-flight RPCs. The server supports three cancel modes:

- `kill` (default) — cancel the handler's asyncio task.
- `skip` — stop waiting for the result, let the handler keep running.
- `killnowait` — cancel the task and respond immediately.

The server can also cancel calls to clients:

```python
@wamp.on_session_open
async def on_open(session: WampSession) -> None:
    # Start a call, then cancel it
    import asyncio
    task = asyncio.create_task(session.call("com.client.slow_op"))
    await asyncio.sleep(1)
    await session.cancel(1)  # cancel by request ID
```

## Session Lifecycle Callbacks

```python
@wamp.on_session_open
async def on_open(session: WampSession) -> None:
    print(f"Session {session.session_id} connected")

@wamp.on_session_close
async def on_close(session: WampSession) -> None:
    print(f"Session {session.session_id} disconnected")
```

## Custom Serializers

The default serializer is JSON. To add a custom serializer, implement the `Serializer` protocol:

```python
from fastapi_headless_wamp import Serializer, register_serializer

class MsgpackSerializer:
    @property
    def protocol(self) -> str:
        return "msgpack"

    @property
    def is_binary(self) -> bool:
        return True

    def encode(self, data: list) -> bytes:
        import msgpack
        return msgpack.packb(data)

    def decode(self, data: str | bytes) -> list:
        import msgpack
        return msgpack.unpackb(data, raw=False)

register_serializer(MsgpackSerializer())
```

Clients can then connect with the `wamp.2.msgpack` subprotocol.

## wampy.js Compatibility

This library is designed to be compatible with [wampy.js](https://github.com/nicola/wampy.js) and other standard WAMP clients. The server 
advertises the following WAMP role features:

**Dealer features:**
- `progressive_call_results`
- `call_canceling`
- `caller_identification`
- `call_timeout`

**Broker features:**
- `publisher_identification`
- `publisher_exclusion`
- `subscriber_blackwhite_listing`

## API Reference

### `WampHub(realm="realm1")`

The central hub that manages WAMP sessions. Key methods and decorators:

| Method / Decorator               | Description                                         |
|----------------------------------|-----------------------------------------------------|
| `@hub.register(uri)`             | Register a server-side RPC handler                  |
| `@hub.subscribe(topic)`          | Register a server-side subscription handler         |
| `hub.register_service(service)`  | Register a `WampService` instance                   |
| `@hub.on_session_open`           | Callback when a session completes handshake         |
| `@hub.on_session_close`          | Callback when a session disconnects                 |
| `hub.handle_websocket(ws)`       | Full async WebSocket handler                        |
| `hub.get_router(path="/ws")`     | Get a FastAPI `APIRouter` with the WAMP endpoint    |
| `hub.publish_to_all(topic, ...)` | Publish an event to all subscribed sessions         |
| `hub.sessions`                   | Dict of active sessions (session_id -> WampSession) |
| `hub.session_count`              | Number of active sessions                           |

### `WampSession`

Represents a single WAMP session. Key methods:

| Method                                     | Description                       |
|--------------------------------------------|-----------------------------------|
| `session.call(uri, args, kwargs, timeout)` | Call a client-registered RPC      |
| `session.cancel(request_id, mode)`         | Cancel a pending call to a client |
| `session.publish(topic, args, kwargs)`     | Publish an event to this client   |

### `WampService`

Base class for grouping RPCs with a URI prefix. Use `@rpc()` and `@subscribe()` to mark methods.

### Error Classes

| Exception                    | WAMP Error URI                        |
|------------------------------|---------------------------------------|
| `WampError`                  | (base class)                          |
| `WampNoSuchProcedure`        | `wamp.error.no_such_procedure`        |
| `WampNoSuchSubscription`     | `wamp.error.no_such_subscription`     |
| `WampRuntimeError`           | `wamp.error.runtime_error`            |
| `WampCallTimeout`            | `wamp.error.canceled`                 |
| `WampCanceled`               | `wamp.error.canceled`                 |
| `WampProcedureAlreadyExists` | `wamp.error.procedure_already_exists` |
| `WampProtocolError`          | (protocol-level error)                |
| `WampInvalidMessage`         | (message validation error)            |

## License

Apache 2.0 — see [LICENSE](LICENSE) for details.
