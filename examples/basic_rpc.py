"""Basic RPC example using standalone decorators.

Run with:
    uvicorn examples.basic_rpc:app --host 0.0.0.0 --port 8000

Clients connect via WebSocket to ws://localhost:8000/ws using the
``wamp.2.json`` subprotocol and can call the registered procedures.
"""

from fastapi import FastAPI
from fastapi_headless_wamp import WampHub, WampSession

app = FastAPI(title="fastapi-headless-wamp Basic RPC Example")
wamp = WampHub(realm="realm1")


# --- Async RPC handler ---


@wamp.register("com.example.add")
async def add(session: WampSession, a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@wamp.register("com.example.greet")
async def greet(session: WampSession, name: str) -> str:
    """Return a greeting for the given name."""
    return f"Hello, {name}!"


# --- Sync RPC handler (runs via asyncio.to_thread automatically) ---


@wamp.register("com.example.fibonacci")
def fibonacci(session: WampSession, n: int) -> int:
    """Compute the n-th Fibonacci number (sync)."""
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a


# Mount the WAMP WebSocket endpoint
app.include_router(wamp.get_router(path="/ws"))
