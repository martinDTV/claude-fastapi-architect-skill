"""P2: framework callbacks are external; typed callables resolve with one verifiable provider only."""

import time
from collections.abc import Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class Audit:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def receive_wrapper() -> Message:
            message = await receive()                                                  # truth-unresolved: external
            return message

        await self.app(scope, receive_wrapper, send)                                   # truth-unresolved: external


class Scanner:
    def read(self, code: str) -> str:
        time.sleep(0.2)
        return code


def run(step: Callable[[str], str], code: str) -> str:
    return step(code)                                                                  # truth-edge: wms.gateway.Scanner.read


async def scan(scanner: Scanner, code: str) -> str:
    return run(scanner.read, code)                                                     # truth: ASYNC_BLOCKING_CALL_INDIRECT


def choose(step: Callable[[str], str], code: str) -> str:
    return step(code)                                                                  # truth-unresolved: ambiguous


def quick(code: str) -> str:
    return code


async def either(scanner: Scanner, code: str) -> str:
    return choose(scanner.read, code) + choose(quick, code)                            # truth-absent: ASYNC_BLOCKING_CALL_INDIRECT
