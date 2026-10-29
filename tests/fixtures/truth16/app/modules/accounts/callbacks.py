"""Functions passed as arguments: resolvable when every caller passes the same project function."""

import time


def slow(x: int) -> int:
    time.sleep(1)
    return x


def fast(x: int) -> int:
    return x * 2


def run_with(handler, x: int) -> int:
    return handler(x)                                                                  # truth-edge: app.modules.accounts.callbacks.slow


def run_either(handler, x: int) -> int:
    return handler(x)                                                                  # truth-unresolved: ambiguous


async def route_like() -> int:
    return run_with(slow, 1)                                                           # truth: ASYNC_BLOCKING_CALL_INDIRECT


async def ambiguous_route() -> int:
    return run_either(slow, 1) + run_either(fast, 2)                                   # truth-absent: ASYNC_BLOCKING_CALL_INDIRECT
