# End-to-End Demo

A complete, runnable demo that starts a **FastAPI + WAMP** server and
connects **two JavaScript WAMP clients** via
[Wampy.js](https://github.com/KSDaemon/wampy.js) to verify that RPC
calls and PubSub events flow in both directions.

## What it demonstrates

| Feature                                      | How                                                                                                                                              |
|----------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------|
| **RPC (client -> server)**                   | Client A calls `com.example.add`, `com.example.greet`, `com.example.time`, `com.example.echo.reverse`, `com.example.echo.stats`                  |
| **PubSub (client -> server -> all clients)** | Client A publishes to `com.example.echo.shout`; the server uppercases the text and re-publishes to `com.example.echo.loud`; Client B receives it |
| **PubSub ping/pong**                         | Client A publishes to `com.example.ping`; the server replies with `com.example.pong` to all sessions                                             |
| **Server push on connect**                   | The server publishes a `com.example.welcome` event to every newly connected session                                                              |
| **Class-based services**                     | `EchoService` uses the `WampService` base class with `@rpc` and `@subscribe` decorators                                                          |
| **Two independent clients**                  | Client A and Client B connect simultaneously, showing that events are broadcast across sessions                                                  |

## Architecture

```
┌───────────────────────────────────────────────────────┐
│              FastAPI + WampHub (server.py)            │
│                                                       │
│  RPCs:                                                │
│    com.example.add(a, b)          -> a + b            │
│    com.example.greet(name)        -> "Hello, name!"   │
│    com.example.time()             -> ISO timestamp    │
│    com.example.echo.reverse(text) -> reversed text    │
│    com.example.echo.stats()       -> {total_calls}    │
│                                                       │
│  Subscriptions (server listens):                      │
│    com.example.echo.shout   -> uppercase + broadcast. │
│    com.example.ping         -> publish pong to all    │
│                                                       │
│  Lifecycle:                                           │
│    on_session_open  -> publish com.example.welcome    │
│    on_session_close -> publish com.example.client_left│
├───────────────────────┬───────────────────────────────┤
│    WebSocket /ws      │    WebSocket /ws              │
│         |             │         |                     │
│   Client A (caller,   │   Client B (subscriber)       │
│    publisher)         │                               │
│   (Wampy.js)          │   (Wampy.js)                  │
└───────────────────────┴───────────────────────────────┘
```

## Prerequisites

- **Python 3.12+** with the project installed (editable or venv)
- **Node.js 18+** and **npm**
- **uvicorn** (included in dev dependencies)

## Quick start

### Automated (recommended)

From the repository root:

```bash
cd examples/e2e
bash run.sh
```

The script will:
1. Install Node.js dependencies (`wampy`, `ws`)
2. Start the FastAPI server on port 8080
3. Run the client script that connects two WAMP sessions
4. Print a summary of all checks (PASS/FAIL)
5. Shut down the server

### Manual

**Terminal 1 — start the server:**

```bash
# From the repository root
uvicorn examples.e2e.server:app --host 0.0.0.0 --port 8080
```

**Terminal 2 — run the client:**

```bash
cd examples/e2e
npm install
node client.mjs ws://localhost:8080/ws
```

### Interactive with Wampy CLI

You can also explore the server interactively using the
[Wampy CLI](https://github.com/KSDaemon/wampy.js#cli-tool):

```bash
# Start the server (Terminal 1)
uvicorn examples.e2e.server:app --host 0.0.0.0 --port 8080

# Call an RPC (Terminal 2)
npx wampy call com.example.add -W ws://localhost:8080/ws -r realm1 --args '[17, 25]'

# Subscribe to a topic
npx wampy subscribe com.example.echo.loud -W ws://localhost:8080/ws -r realm1

# Publish to a topic (Terminal 3)
npx wampy publish com.example.echo.shout -W ws://localhost:8080/ws -r realm1 --args '["hello"]'
```

## Files

| File           | Description                                                                                    |
|----------------|------------------------------------------------------------------------------------------------|
| `server.py`    | FastAPI application with RPC handlers, PubSub subscriptions, and session lifecycle hooks       |
| `client.mjs`   | Node.js script using Wampy.js that connects two WAMP clients and runs all demo steps           |
| `run.sh`       | Shell script that orchestrates the full demo (install deps, start server, run client, cleanup) |
| `package.json` | Node.js dependencies (wampy, ws)                                                               |
| `README.md`    | This file                                                                                      |
