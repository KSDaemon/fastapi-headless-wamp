# PRD: fastapi-headless-wamp

## Introduction

**Type:** Feature

A Python library (`fastapi-headless-wamp`) that brings full bidirectional WAMP (Web Application Messaging Protocol) RPC and PubSub capabilities to FastAPI applications **without requiring a separate WAMP router**.

**The key architectural insight:** Each WebSocket connection is an **isolated peer-to-peer WAMP session** between the FastAPI server and one client. There is no cross-client routing. The server and client are two WAMP peers talking directly over one WebSocket:

- **Server registers RPCs** that the client can call (server = callee, client = caller)
- **Client registers RPCs** that the server can call (client = callee, server = caller)
- **Server publishes events** to a specific client's session (if that client has subscribed)
- **Client publishes events** to the server (if the server has subscribed on that session)
- **No broker/router**: no message forwarding between clients, no shared topic space across connections

This is a **full WAMP peer implementation** embedded in FastAPI, not a stripped-down subset. It supports progressive call results, call cancellation, and the complete WAMP session lifecycle -- everything a standard WAMP client like wampy.js expects from its peer.

**The core problem:** Chat and real-time features in web applications currently rely on raw WebSocket handlers with manual message parsing, type dispatch, and response formatting. This is fragile, hard to maintain, and lacks a standardized protocol. WAMP solves this with a well-defined protocol for RPC and PubSub over WebSocket, but existing WAMP implementations (Crossbar.io, Autobahn) require a separate router process and are built around a centralized hub-and-spoke model that doesn't fit the typical web app architecture where each client talks to the server.

**The solution:** Embed a lightweight WAMP peer directly inside FastAPI. Each WebSocket connection gets its own isolated WAMP session. The server and client can both register RPCs and subscribe to topics within that session. Standard WAMP clients connect and everything "just works" per the WAMP protocol.

## Goals

- Provide a pip-installable PyPI package (`fastapi-headless-wamp`)
- Implement **full WAMP session lifecycle** (HELLO/WELCOME/GOODBYE/ABORT) compatible with standard WAMP clients
- **Bidirectional RPC**: server registers RPCs callable by client, AND server can call RPCs registered by client
- **Progressive call results**: support `receive_progress` option on CALL and progressive YIELD from callee (client->server direction first, designed for both)
- **Call cancellation**: support CANCEL/INTERRUPT message flow
- **Bidirectional PubSub**: both server and client can subscribe to topics and publish events, all within the isolated session
- Offer two API styles: standalone function decorators AND class-based service registration
- Pluggable serialization (JSON default, architecture for MsgPack/CBOR)
- Integrate with FastAPI via both `handle_websocket()` and `include_router()` patterns
- Full compatibility with wampy.js and any WAMP-compliant client
- No authentication in Phase 1 (extensible architecture for adding later)

## Architecture: Peer-to-Peer Model

```
  ┌─────────────────────────────────────────────────────┐
  │                   FastAPI Server                      │
  │                                                       │
  │  ┌──────────┐   ┌──────────┐   ┌──────────┐         │
  │  │ Session 1 │   │ Session 2 │   │ Session 3 │  ...   │
  │  │ (peer)    │   │ (peer)    │   │ (peer)    │         │
  │  └─────┬─────┘   └─────┬─────┘   └─────┬─────┘         │
  └────────┼──────────────┼──────────────┼───────────────┘
           │              │              │
        WebSocket      WebSocket      WebSocket
           │              │              │
  ┌────────┴───┐  ┌──────┴─────┐  ┌────┴───────┐
  │  Client 1  │  │  Client 2  │  │  Client 3  │
  │  (peer)    │  │  (peer)    │  │  (peer)    │
  │  wampy.js  │  │  wampy.js  │  │  wampy.js  │
  └────────────┘  └────────────┘  └────────────┘

  Each line is an ISOLATED peer-to-peer WAMP session.
  No messages cross between sessions.
```

**Everything is per-session.** Each `Session` object is a fully isolated WAMP peer:
- Has its own **server-side RPC instances** (defined once on the hub via decorators, but dispatched per-session -- each session has its own context)
- Has its own **client-registered RPC map** (RPCs the client registered via REGISTER, callable by server via `session.call()`)
- Has its own **subscription map** (topics the client subscribed to, plus topics the server subscribes to on that session)
- Has its own **request ID tracking** for pending calls in both directions

There is **no global namespace**. If 100 clients each register `com.myapp.get_user_answer`, that's 100 separate registrations, one per session. When the server calls `session.call("com.myapp.get_user_answer")`, it calls the RPC on that specific client only.

The `WampHub` holds:
- **Server-side RPC definitions** (registered at startup via decorators -- these are *templates*, each session gets them)
- **Server-side subscription handler definitions** (registered via `@wamp.subscribe` -- also per-session when dispatched)
- **Active sessions dict** (for the server to look up a session and call client RPCs or publish to it)
- **Logging**: uses Python's standard `logging` module (`logging.getLogger("fastapi_headless_wamp")`)

## User Stories

### US-001: Project Setup and Package Structure
**Description:** As a developer, I need the project scaffolding so that the library can be developed, tested, and published to PyPI.

**Acceptance Criteria:**
- [ ] `pyproject.toml` with: package metadata, `requires-python = ">=3.12"`, dependencies (fastapi, starlette), dev dependencies (pytest, pytest-asyncio, httpx, uvicorn)
- [ ] **uv** as the package/task manager for everything: dependency management, virtual env, running tests, linting, building
- [ ] Source layout: `src/fastapi_headless_wamp/` package directory
- [ ] Module structure: `__init__.py`, `hub.py`, `session.py`, `protocol.py`, `serializers.py`, `service.py`, `types.py`, `errors.py`
- [ ] **Apache 2.0 license**: `LICENSE` file + license field in `pyproject.toml` + NOTICE file
- [ ] `.gitignore` for Python projects
- [ ] `.python-version` set to `3.14` (development version)
- [ ] Package is installable locally with `uv pip install -e .`
- [ ] **Public API exports** in `__init__.py`: `WampHub`, `WampSession`, `WampService`, `rpc`, `subscribe`, `Serializer`, `JsonSerializer`, error classes
- [ ] **Logging**: configure `logging.getLogger("fastapi_headless_wamp").addHandler(logging.NullHandler())` in `__init__.py`. Each module uses `logger = logging.getLogger(__name__)`. No handlers/formatters set -- the application controls logging configuration.
- [ ] Typecheck passes (mypy or pyright configured)

### US-002: WAMP Protocol Constants and Message Types
**Description:** As a developer, I need WAMP message type constants and message structures so that the protocol layer can encode/decode messages correctly.

**Acceptance Criteria:**
- [ ] All WAMP message type codes as IntEnum: HELLO=1, WELCOME=2, ABORT=3, GOODBYE=6, ERROR=8, PUBLISH=16, PUBLISHED=17, SUBSCRIBE=32, SUBSCRIBED=33, UNSUBSCRIBE=34, UNSUBSCRIBED=35, EVENT=36, CALL=48, CANCEL=49, RESULT=50, REGISTER=64, REGISTERED=65, UNREGISTER=66, UNREGISTERED=67, INVOCATION=68, INTERRUPT=69, YIELD=70
- [ ] Message validation functions for each type (correct array length, field types)
- [ ] WAMP error URI constants: `wamp.error.no_such_procedure`, `wamp.error.invalid_argument`, `wamp.error.runtime_error`, `wamp.error.no_such_subscription`, `wamp.error.procedure_already_exists`, `wamp.error.canceled`, etc.
- [ ] Type aliases/dataclasses for message structures
- [ ] Unit tests for message validation

### US-003: Pluggable Serialization System
**Description:** As a developer, I need a serializer abstraction so that JSON is used by default but other formats can be added without modifying core code.

**Acceptance Criteria:**
- [ ] `Serializer` protocol (typing.Protocol) with: `encode(data: list) -> str | bytes`, `decode(data: str | bytes) -> list`, `protocol: str` property, `is_binary: bool` property
- [ ] `JsonSerializer` implementation: protocol="json", is_binary=False
- [ ] Global serializer registry: `register_serializer(serializer)`, `get_serializer(protocol) -> Serializer`, `get_available_subprotocols() -> list[str]`
- [ ] WAMP subprotocol string: `wamp.2.{protocol}` (e.g., `wamp.2.json`)
- [ ] Unit tests for JSON serializer encode/decode with WAMP message arrays
- [ ] Typecheck passes

### US-004: WAMP Session Management
**Description:** As a developer, I need a WampSession class that wraps a FastAPI WebSocket and manages the full WAMP session lifecycle per connection.

**Acceptance Criteria:**
- [ ] `WampSession` class wrapping `starlette.websockets.WebSocket`
- [ ] Subprotocol negotiation during WebSocket accept (picks serializer from `Sec-WebSocket-Protocol` header)
- [ ] Receives HELLO, validates realm, generates session ID, sends WELCOME with server features. The WELCOME details dict must match the structure wampy.js expects:
  ```python
  {
      "roles": {
          "dealer": {
              "features": {
                  "progressive_call_results": True,
                  "call_canceling": True,
                  "caller_identification": True,
                  "call_timeout": True,
              }
          },
          "broker": {
              "features": {
                  "publisher_identification": True,
                  "publisher_exclusion": True,
                  "subscriber_blackwhite_listing": True,
              }
          }
      }
  }
  ```
- [ ] Handles GOODBYE from client: sends GOODBYE back, closes cleanly
- [ ] Handles ABORT: sends ABORT, closes
- [ ] Session ID: random integer, unique across active sessions (per WAMP spec)
- [ ] `send_message(msg_list)` and `receive_message() -> msg_list` using negotiated serializer
- [ ] Async iterator (`async for message in session:`) for message loop
- [ ] **All state is per-session and isolated**: own subscription map, own client-registered RPC map, own pending-call tracking (both directions), own request ID counters. No global/shared namespace.
- [ ] Proper cleanup on disconnect
- [ ] Tests for handshake and teardown
- [ ] Typecheck passes

### US-005: Server-Side RPC Registration (Standalone Decorators)
**Description:** As a backend developer, I want to register Python functions as WAMP RPCs using decorators, so that connected WAMP clients can call them.

**Acceptance Criteria:**
- [ ] `WampHub.register(uri: str)` decorator method
- [ ] Decorated async functions stored as **RPC definitions** on the hub (templates available to every session)
- [ ] Each session dispatches CALL to the definition -- the handler executes in that session's context
- [ ] Client sends CALL -> server invokes function with `args`/`kwargs` -> sends RESULT back
- [ ] Both **async and sync** functions supported as RPC handlers (sync handlers run in the default executor via `asyncio.to_thread()`)
- [ ] Exception in handler -> ERROR message with `wamp.error.runtime_error`
- [ ] Non-existent URI -> ERROR with `wamp.error.no_such_procedure`
- [ ] Request IDs tracked and matched between CALL and RESULT/ERROR
- [ ] **`call_timeout`**: if client sends `timeout` in CALL options (milliseconds), server cancels handler after that duration and returns ERROR `wamp.error.canceled`
- [ ] **`caller_identification`**: if client sends `disclose_me: true` in CALL options, pass `details.caller = session_id` in the handler's invocation context
- [ ] Example:
  ```python
  wamp = WampHub(realm="realm1")

  @wamp.register("com.myapp.add")
  async def add(a: int, b: int) -> int:
      return a + b

  # Sync functions also work:
  @wamp.register("com.myapp.compute")
  def compute(x: float) -> float:
      return x ** 2
  ```
- [ ] Tests for: success, exception, non-existent procedure, timeout, sync handler
- [ ] Typecheck passes

### US-006: Server-Side RPC Registration (Class-Based Services)
**Description:** As a backend developer, I want to group related RPCs into service classes with a URI prefix.

**Acceptance Criteria:**
- [ ] `WampService` base class with optional `prefix` class attribute
- [ ] `@rpc(uri)` decorator for methods; `@rpc` without args infers URI from method name
- [ ] Both **async and sync** methods supported (sync methods run via `asyncio.to_thread()`)
- [ ] Full URI = `prefix + "." + uri` (or `prefix + "." + method_name` if no explicit URI)
- [ ] `WampHub.register_service(service_instance)` introspects and registers all marked methods
- [ ] Service has access to the hub for publishing: `self.hub.publish(session, topic, ...)`
- [ ] Example:
  ```python
  class ChatService(WampService):
      prefix = "com.myapp.chat"

      @rpc("send_message")
      async def send_message(self, text: str, channel_id: int) -> dict:
          return {"status": "sent"}

      @rpc  # -> com.myapp.chat.get_history
      async def get_history(self, channel_id: int) -> list:
          return []
  ```
- [ ] Tests for: service registration, invocation, prefix resolution
- [ ] Typecheck passes

### US-007: Server Calling Client-Registered RPCs
**Description:** As a backend developer, I want to call RPCs that a WAMP client has registered on its side, so that I can invoke client-side functions (e.g., trigger UI updates, request data from the browser).

**Acceptance Criteria:**
- [ ] Client sends REGISTER -> server stores registration in **that session's** client-RPC map, sends REGISTERED
- [ ] Client sends UNREGISTER -> server removes from **that session's** map, sends UNREGISTERED
- [ ] Multiple clients can register the same URI independently (each in their own session, no conflict)
- [ ] `session.call(uri, args=None, kwargs=None) -> result` async method on WampSession
- [ ] Server sends INVOCATION to **that specific client**, awaits YIELD/ERROR response, returns result or raises
- [ ] Proper request ID tracking for server-initiated calls (separate ID space from client-initiated)
- [ ] Timeout support: if client doesn't respond within timeout, raise `WampCallTimeout`
- [ ] `wamp.error.no_such_procedure` if calling an unregistered URI on **that session**
- [ ] Example:
  ```python
  # Server-side code, somewhere in your business logic:
  session = wamp.sessions[some_session_id]
  result = await session.call("com.myapp.client.get_viewport_size")
  # result contains whatever the JS client returned
  ```
- [ ] Tests for: successful call, client error response, timeout, non-existent procedure
- [ ] Typecheck passes

### US-008: Progressive Calls (Results and Invocations)
**Description:** As a developer, I want to support progressive call results (callee sends intermediate results) and progressive call invocations (caller sends input data in chunks), so that long-running RPCs can stream data in both directions.

**Acceptance Criteria:**
- [ ] **Progressive Results -- Client->Server (client calls server RPC with `receive_progress: true`):**
  - Server RPC handler receives a `progress` callback it can call to send intermediate results
  - Each progress call sends RESULT with `options.progress = true`
  - Final return value sends RESULT without progress flag
  - Example:
    ```python
    @wamp.register("com.myapp.long_task")
    async def long_task(n: int, _progress=None) -> str:
        for i in range(n):
            if _progress:
                await _progress({"step": i, "total": n})
            await asyncio.sleep(0.1)
        return "done"
    ```
- [ ] **Progressive Results -- Server->Client (server calls client RPC):**
  - `session.call(uri, ..., receive_progress=True, on_progress=callback)` sends INVOCATION with `receive_progress: true`
  - Progressive YIELD messages from client trigger `on_progress` callback
  - Final YIELD resolves the call
- [ ] **Progressive Invocations -- Client->Server (wampy.js `progressiveCall()`):**
  - Client sends multiple CALL messages with the **same request ID** and `options.progress = true`
  - Server accumulates/streams these to the RPC handler (e.g., via an async iterator or callback)
  - Final CALL from client has `options.progress = false` (or absent), signaling end of input
  - Handler then produces its result(s)
- [ ] Request ID properly tracked throughout progressive exchange
- [ ] Tests for progressive results in both directions, progressive invocations
- [ ] Typecheck passes

### US-009: Call Cancellation
**Description:** As a developer, I want to support WAMP call cancellation so that long-running RPCs can be interrupted.

**Acceptance Criteria:**
- [ ] **Client cancels a call to server:**
  - Client sends CANCEL [request_id, options]
  - Server sends INTERRUPT to the running handler (via asyncio task cancellation or a cancellation token)
  - If handler supports cancellation, it stops and server sends ERROR with `wamp.error.canceled`
  - If handler already completed, CANCEL is ignored
  - `options.mode` support: "skip" (just stop waiting), "kill" (abort handler), "killnowait" (abort, don't wait for cleanup)
- [ ] **Server cancels a call to client:**
  - `session.cancel(request_id)` sends CANCEL to client
  - Client responds with ERROR `wamp.error.canceled` or completes normally
  - **Known wampy.js limitation**: wampy.js as a callee does NOT handle INTERRUPT messages (commented out in its message handler). Server-initiated cancellation sends CANCEL (server acts as caller), but the client may not abort a running handler mid-execution. The call will still complete or time out.
- [ ] Tests for: cancel before completion, cancel after completion, cancel modes
- [ ] Typecheck passes

### US-010: PubSub -- Client Subscribe/Unsubscribe
**Description:** As a WAMP client, I want to subscribe to topics so I receive events the server publishes to my session.

**Acceptance Criteria:**
- [ ] Client sends SUBSCRIBE [request_id, options, topic] -> server stores in session's subscription map, sends SUBSCRIBED [request_id, subscription_id]
- [ ] Client sends UNSUBSCRIBE [request_id, subscription_id] -> server removes, sends UNSUBSCRIBED [request_id]
- [ ] Subscriptions are **per-session** (isolated, not shared across clients)
- [ ] Subscription IDs unique within the session
- [ ] UNSUBSCRIBE for non-existent subscription -> ERROR `wamp.error.no_such_subscription`
- [ ] Cleanup all subscriptions when session disconnects
- [ ] Tests for subscribe, unsubscribe, non-existent unsubscribe
- [ ] Typecheck passes

### US-011: PubSub -- Server Publishes to Client
**Description:** As a backend developer, I want to publish events to a specific client's session, so that the client receives them if subscribed.

**Acceptance Criteria:**
- [ ] `session.publish(topic, args=None, kwargs=None)` async method on WampSession
- [ ] Sends EVENT only if the client has subscribed to that topic on this session
- [ ] EVENT format: [EVENT, subscription_id, publication_id, details, args_list, args_dict]
- [ ] **`publisher_identification`**: optionally include `details.publisher` in EVENT if the client subscribed with publisher identification support
- [ ] Publication IDs unique and incrementing
- [ ] If client not subscribed, publish is a no-op (no error)
- [ ] Convenience on hub: `wamp.publish(session_id, topic, ...)` or `wamp.publish_to_all(topic, ...)` (for broadcasting to all sessions that have subscribed)
- [ ] Example:
  ```python
  # Publish to a specific client session:
  session = wamp.sessions[session_id]
  await session.publish("com.myapp.chat.new_message", args=[{"text": "hello"}])

  # Publish to all sessions that subscribed to this topic:
  await wamp.publish_to_all("com.myapp.notifications", args=[{"msg": "server restart in 5m"}])
  ```
- [ ] Tests for: publish to subscribed client, publish to unsubscribed (no-op), publish_to_all
- [ ] Typecheck passes

### US-012: PubSub -- Client Publishes, Server Receives
**Description:** As a backend developer, I want the server to subscribe to topics and handle events that clients publish, so the server can react to client-initiated events.

**Acceptance Criteria:**
- [ ] `@wamp.subscribe(topic)` decorator for server-side event handlers
- [ ] When a client sends PUBLISH on a topic the server has a handler for, the handler is invoked
- [ ] If `options.acknowledge` is true, send PUBLISHED [request_id, publication_id] back to client
- [ ] Server subscription handlers receive the event args/kwargs plus the session that published
- [ ] **`publisher_exclusion`**: handle `exclude_me` option from client PUBLISH (in peer-to-peer model this is trivial -- there's only one subscriber per session, but implement correctly for spec compliance)
- [ ] **`subscriber_blackwhite_listing`**: handle `eligible`/`exclude` options from client PUBLISH (trivial in peer-to-peer, but parse and respect for correctness)
- [ ] Example:
  ```python
  @wamp.subscribe("com.myapp.chat.typing")
  async def on_typing(session: WampSession, user_id: int, channel_id: int):
      # React to client typing event
      pass
  ```
- [ ] Also works in class-based services with `@subscribe(topic)` decorator
- [ ] Tests for: client publish -> server handler invoked, acknowledge mode
- [ ] Typecheck passes

### US-013: FastAPI Integration -- Manual WebSocket Handler
**Description:** As a backend developer, I want to use `wamp.handle_websocket(websocket)` in my own endpoint for full control.

**Acceptance Criteria:**
- [ ] `WampHub.handle_websocket(websocket: WebSocket)` async coroutine
- [ ] Performs: accept with subprotocol -> HELLO/WELCOME handshake -> message loop -> cleanup
- [ ] **Blocks until the client disconnects** (runs the message loop). The session is accessible during the connection via `wamp.sessions` dict or the `on_session_open` / `on_session_close` lifecycle callbacks on the hub.
- [ ] Example:
  ```python
  wamp = WampHub(realm="realm1")

  @wamp.on_session_open
  async def session_opened(session: WampSession):
      # Session is now active, can be stored, used for server->client calls, etc.
      print(f"Client connected: session {session.id}")

  @wamp.on_session_close
  async def session_closed(session: WampSession):
      print(f"Client disconnected: session {session.id}")

  @app.websocket("/ws")
  async def ws_endpoint(websocket: WebSocket):
      await wamp.handle_websocket(websocket)
      # Returns only when client disconnects
  ```
- [ ] Integration test with FastAPI test client
- [ ] Typecheck passes

### US-014: FastAPI Integration -- Auto-Mount via Router
**Description:** As a backend developer, I want to auto-mount the endpoint using `include_router()` for simple setups.

**Acceptance Criteria:**
- [ ] `WampHub.get_router(path: str = "/ws") -> APIRouter`
- [ ] Returns a pre-configured APIRouter
- [ ] Example: `app.include_router(wamp.get_router(path="/ws"))`
- [ ] Integration test
- [ ] Typecheck passes

### US-015: Error Handling and Protocol Violations
**Description:** Robust error handling for protocol violations, invalid messages, and runtime errors.

**Acceptance Criteria:**
- [ ] Exception hierarchy: `WampError` base, `WampProtocolError`, `WampInvalidMessage`, `WampNoSuchProcedure`, `WampNoSuchSubscription`, `WampRuntimeError`, `WampCallTimeout`, `WampCanceled`
- [ ] Invalid messages -> ABORT or ERROR (never crash)
- [ ] ERROR for: no_such_procedure, invalid_argument, runtime_error, no_such_subscription, canceled, procedure_already_exists
- [ ] Protocol violations -> session ABORT
- [ ] Disconnects handled cleanly (pending calls rejected, subscriptions cleaned up)
- [ ] All error paths tested
- [ ] Typecheck passes

### US-016: Connection and Session Tracking
**Description:** As a backend developer, I want to access active sessions to call client RPCs or publish to specific clients.

**Acceptance Criteria:**
- [ ] `WampHub.sessions -> dict[int, WampSession]` (session_id -> session)
- [ ] `WampHub.session_count -> int`
- [ ] Sessions added on WELCOME, removed on disconnect/GOODBYE
- [ ] Session lifecycle callbacks: `on_session_open(session)`, `on_session_close(session)` (configurable on hub)
- [ ] asyncio-safe access
- [ ] Tests for lifecycle tracking
- [ ] Typecheck passes

### US-017: Comprehensive Test Suite
**Description:** Reliable test suite covering all functionality.

**Acceptance Criteria:**
- [ ] pytest + pytest-asyncio, run via `uv run pytest`
- [ ] Unit tests: protocol, serializers, message validation, session lifecycle, RPC dispatch (both directions), PubSub, progressive calls, progressive invocations, cancellation, call_timeout, caller_identification, error handling, sync handler support
- [ ] Integration tests with FastAPI WebSocket test client simulating wampy.js message sequences
- [ ] Test coverage > 80%
- [ ] Typecheck passes
- [ ] **Note**: End-to-end tests with actual wampy.js client (Node.js) are a separate future phase, added after the core library is stable

### US-018: Package Documentation and Examples
**Description:** README and examples so users can get started quickly.

**Acceptance Criteria:**
- [ ] README.md: description, install, quick start (both API styles), architecture diagram
- [ ] Example: `examples/basic_rpc.py` -- standalone decorator RPCs
- [ ] Example: `examples/class_service.py` -- class-based service
- [ ] Example: `examples/bidirectional.py` -- server calling client RPCs
- [ ] Example: `examples/pubsub.py` -- subscribe/publish both directions
- [ ] Note about wampy.js compatibility

## Functional Requirements

- FR-1: Implement full WAMP session lifecycle: HELLO, WELCOME, GOODBYE, ABORT
- FR-2: Support server-side RPC registration via `@wamp.register(uri)` decorator on standalone async functions
- FR-3: Support server-side RPC registration via `@rpc(uri)` on methods of `WampService` subclasses
- FR-4: Client CALL for registered URI -> invoke function -> send RESULT
- FR-5: Client CALL for unregistered URI -> ERROR `wamp.error.no_such_procedure`
- FR-6: RPC handler exception -> ERROR `wamp.error.runtime_error` with details
- FR-7: Client REGISTER -> store in **that session's** client-RPC map (not global) -> REGISTERED
- FR-8: Client UNREGISTER -> remove from **that session's** map -> UNREGISTERED
- FR-9: `session.call(uri, ...)` looks up URI in **that session's** client-RPC map, sends INVOCATION to that client, awaits YIELD/ERROR
- FR-10: Progressive call results: RESULT/YIELD with `progress=true` for intermediate results, final RESULT/YIELD without flag (both directions)
- FR-10a: Progressive call invocations: multiple CALL messages with same request ID and `progress=true`, handler receives streamed input
- FR-11: Call cancellation: CANCEL from caller -> INTERRUPT to handler -> ERROR `canceled` (with skip/kill/killnowait modes)
- FR-11a: Call timeout: respect `timeout` option in CALL, cancel handler after duration, return ERROR `canceled`
- FR-12: Client SUBSCRIBE -> store in **that session's** subscription map -> SUBSCRIBED
- FR-13: Client UNSUBSCRIBE -> remove from **that session's** map -> UNSUBSCRIBED
- FR-14: `session.publish(topic, ...)` sends EVENT to client only if **that client** has subscribed on **that session**
- FR-15: `wamp.publish_to_all(topic, ...)` sends EVENT to all sessions where client is subscribed to topic
- FR-16: Client PUBLISH -> invoke server-side `@subscribe` handler if registered
- FR-17: Pluggable serialization via `Serializer` protocol; JSON included by default
- FR-18: WebSocket subprotocol negotiation based on client-requested protocols
- FR-19: `handle_websocket(ws)` for manual FastAPI integration
- FR-20: `get_router(path)` returning APIRouter for auto-mount
- FR-21: Session IDs: random integers, unique across active sessions
- FR-22: Request IDs tracked per-session, separate spaces for client-initiated and server-initiated calls
- FR-23: Full cleanup on disconnect: pending calls rejected, subscriptions removed, session removed from hub
- FR-24: asyncio-safe: multiple concurrent WebSocket connections handled correctly
- FR-25: **Complete per-session isolation**: all RPC registrations (both server-side and client-side), all subscriptions, all pending calls, all request ID counters are scoped to the individual session. Multiple clients registering the same URI or subscribing to the same topic create independent registrations in their respective sessions. No global/shared namespace exists.
- FR-26: **`caller_identification`**: when client sends `disclose_me: true` in CALL options, include `caller` session ID in INVOCATION details
- FR-27: **`publisher_identification`**: when client sends `disclose_me: true` in PUBLISH options, include `publisher` session ID in EVENT details
- FR-28: **`publisher_exclusion`**: handle `exclude_me` option in client PUBLISH
- FR-29: **`subscriber_blackwhite_listing`**: handle `eligible`/`exclude` options in client PUBLISH
- FR-30: Both sync and async functions supported as RPC handlers (sync run via `asyncio.to_thread()`)
- FR-31: Structured logging via Python `logging` module; no handlers configured by the library

## Non-Goals (Out of Scope)

- **No WAMP authentication** (CHALLENGE/AUTHENTICATE) in Phase 1 -- architecture allows adding later
- **No pattern-based subscriptions/registrations** (prefix/wildcard matching) -- exact match only in Phase 1
- **No cross-client routing**: sessions are isolated, no message forwarding between clients
- **No shared registration**: each server-side RPC has one handler (no load-balancing across callees)
- **No payload passthru mode (PPT)**: no end-to-end encryption in Phase 1
- **No binary serializers in Phase 1**: MsgPack/CBOR addable via pluggable system
- **No HTTP long-poll or raw TCP transports**: WebSocket only
- **No Django, Flask, or other framework support**: FastAPI/Starlette only
- **No persistence**: all state is in-memory

## Technical Considerations

### Dependencies
- **Python**: >= 3.12 (developing on 3.14)
- **Runtime**: `fastapi` (>= 0.100), `starlette` (transitive)
- **Dev**: `pytest`, `pytest-asyncio`, `httpx`, `uvicorn`, `mypy`/`pyright`
- **Tooling**: `uv` for everything -- package management, venv, test running, building, publishing
- **License**: Apache 2.0
- No heavy external dependencies

### Architecture
- `WampHub`: central orchestrator -- holds **server-side RPC/subscribe definitions** (registered at startup via decorators -- these are templates, not live state), active sessions dict, lifecycle callbacks
- `WampSession`: the core unit -- wraps one WebSocket, **owns all live state for that connection**:
  - Server-side RPC handlers (copied from hub definitions at session start)
  - Client-registered RPC map (populated when client sends REGISTER)
  - Client subscription map (populated when client sends SUBSCRIBE)
  - Server subscription handlers (copied from hub definitions)
  - Pending call futures (both directions)
  - Request ID counters (separate for client-initiated and server-initiated)
- `Serializer` protocol: pluggable, JSON default
- Message dispatch: dict mapping message type codes to async handler methods
- All I/O is async (`asyncio`)
- **Key invariant**: no mutable state is shared between sessions. The hub holds definitions and a sessions dict, nothing else.

### Concurrency
- Each WebSocket = one asyncio task (handled by uvicorn/FastAPI)
- Hub state is shared but only mutated from the event loop (asyncio-safe)
- `session.call()` uses asyncio.Future for awaiting client responses
- Call cancellation uses `asyncio.Task.cancel()` or a cancellation token pattern for RPC handlers

### Logging
- Standard Python `logging` module, following library best practices
- Top-level: `logging.getLogger("fastapi_headless_wamp").addHandler(logging.NullHandler())` in `__init__.py`
- Per-module: `logger = logging.getLogger(__name__)` (e.g., `fastapi_headless_wamp.hub`, `fastapi_headless_wamp.session`)
- No handlers, formatters, or levels configured by the library -- the application controls all logging settings
- Log levels used: DEBUG for protocol messages/dispatch, INFO for session open/close, WARNING for recoverable errors, ERROR for handler exceptions
- Users enable debug logging with: `logging.getLogger("fastapi_headless_wamp").setLevel(logging.DEBUG)`

### WAMP Compatibility
- Messages must be valid WAMP arrays parseable by wampy.js
- WELCOME announces: dealer role (with `progressive_call_results`, `call_canceling`, `caller_identification`, `call_timeout`), broker role (with `publisher_identification`, `publisher_exclusion`, `subscriber_blackwhite_listing`)
- Session must handle wampy.js HELLO format (roles + features announcement)
- Must correctly handle wampy.js's `progressiveCall()` (multiple CALL with same request ID + progress flag)
- **Known wampy.js limitation**: callee side does not handle INTERRUPT messages -- documented, not a blocker

### Project Structure
```
fastapi-headless-wamp/
  src/
    fastapi_headless_wamp/
      __init__.py          # Public API exports
      hub.py               # WampHub (orchestrator, server-side registries)
      session.py           # WampSession (per-connection peer, message dispatch)
      protocol.py          # Message types, constants, validation
      serializers.py       # Serializer protocol + JSON implementation
      service.py           # WampService base class + @rpc, @subscribe decorators
      errors.py            # Exception hierarchy
      types.py             # Type aliases
  tests/
    conftest.py
    test_protocol.py
    test_serializers.py
    test_session.py
    test_rpc.py
    test_rpc_reverse.py    # Server calling client RPCs
    test_pubsub.py
    test_progressive.py
    test_cancellation.py
    test_integration.py
    test_service.py
  examples/
    basic_rpc.py
    class_service.py
    bidirectional.py
    pubsub.py
  pyproject.toml
  README.md
  LICENSE              # Apache 2.0
  NOTICE               # Attribution notice (required by Apache 2.0)
  .gitignore
  .python-version      # 3.14
```

## Success Metrics

- wampy.js client can connect and: establish session, call server RPCs, register RPCs callable by server, subscribe/publish in both directions, use progressive calls, cancel calls
- RPC round-trip overhead < 1ms (library processing only)
- 100+ concurrent WebSocket connections without degradation
- Zero protocol violations from wampy.js perspective
- Tests pass with > 80% coverage
- Clean `uv pip install fastapi-headless-wamp` (and regular `pip install` for end users)

## Open Questions

1. **Realm handling**: Single realm sufficient for Phase 1? (Recommendation: yes, single realm, since each session is isolated anyway)
2. **FastAPI dependency injection**: Should RPC handlers support `Depends()`? (Recommendation: Phase 2 -- powerful but complex)
3. **Invocation context**: Should server-side RPC handlers receive caller session info? (Recommendation: optional `WampContext` parameter, passed if handler signature includes it)
4. **Event retention**: Support last-value cache for late subscribers? (Recommendation: out of scope, application-level)
5. **Progressive call direction**: Currently designed for both directions. Should we defer server->client progressive calls? (Recommendation: implement client->server first, server->client is natural extension with same mechanism)
