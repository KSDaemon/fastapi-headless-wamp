"""Class-based WampService example.

Run with:
    uvicorn examples.class_service:app --host 0.0.0.0 --port 8000

Demonstrates grouping related RPCs into a service class with a URI prefix.
Clients can call procedures like ``com.example.math.add``,
``com.example.math.multiply``, and ``com.example.strings.upper``.
"""

from fastapi import FastAPI
from fastapi_headless_wamp import WampHub, WampService, rpc

app = FastAPI(title="fastapi-headless-wamp Class Service Example")
wamp = WampHub(realm="realm1")


# --- Math service ---


class MathService(WampService):
    """Arithmetic operations under the ``com.example.math`` prefix."""

    prefix = "com.example.math"

    @rpc()
    async def add(self, a: int, b: int) -> int:
        """Add two numbers. URI: com.example.math.add"""
        return a + b

    @rpc("multiply")
    async def mul(self, a: int, b: int) -> int:
        """Multiply two numbers. URI: com.example.math.multiply"""
        return a * b

    @rpc()
    def factorial(self, n: int) -> int:
        """Compute factorial (sync handler). URI: com.example.math.factorial"""
        result = 1
        for i in range(2, n + 1):
            result *= i
        return result


# --- String service ---


class StringService(WampService):
    """String utilities under the ``com.example.strings`` prefix."""

    prefix = "com.example.strings"

    @rpc()
    async def upper(self, text: str) -> str:
        """Convert to uppercase. URI: com.example.strings.upper"""
        return text.upper()

    @rpc()
    async def reverse(self, text: str) -> str:
        """Reverse a string. URI: com.example.strings.reverse"""
        return text[::-1]


# Register services with the hub
wamp.register_service(MathService())
wamp.register_service(StringService())

# Mount the WAMP WebSocket endpoint
app.include_router(wamp.get_router(path="/ws"))
