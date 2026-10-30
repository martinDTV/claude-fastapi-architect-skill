"""P2: framework callbacks are external; typed callables resolve only with one verifiable provider."""

import time
from collections.abc import Callable

from starlette.types import ASGIApp, Receive, Scope, Send


class BodyLimit:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        message = await receive()                                                      # truth-unresolved: external
        if message.get("more_body"):                                                   # truth-unresolved: external
            await send({"type": "http.response.start", "status": 413})                 # truth-unresolved: external
        await self.app(scope, receive, send)                                           # truth-unresolved: external


def slow(x: int) -> int:
    time.sleep(1)
    return x


def run_with(handler: Callable[[int], int], x: int) -> int:
    return handler(x)                                                                  # truth-edge: svc.callbacks.slow


async def route_like() -> int:
    return run_with(slow, 1)                                                           # truth: ASYNC_BLOCKING_CALL_INDIRECT


def run_lambda(x: int) -> int:
    return run_with(lambda v: slow(v), x)                                              # the lambda forwards to the same target: still unique


def apply(fn: Callable[[int], int], v: int) -> int:
    return fn(v)                                                                       # truth-edge: svc.callbacks.Service.handle


class Service:
    def handle(self, x: int) -> int:
        time.sleep(1)
        return x

    async def dispatch(self) -> int:
        return apply(self.handle, 1)                                                   # truth: ASYNC_BLOCKING_CALL_INDIRECT


def either(handler: Callable[[int], int], x: int) -> int:
    return handler(x)                                                                  # truth-unresolved: ambiguous


def fast(x: int) -> int:
    return x


async def ambiguous_route() -> int:
    return either(slow, 1) + either(fast, 2)                                           # truth-absent: ASYNC_BLOCKING_CALL_INDIRECT


def no_caller(cb: Callable[[], None]) -> None:
    cb()                                                                               # truth-unresolved: dynamic
