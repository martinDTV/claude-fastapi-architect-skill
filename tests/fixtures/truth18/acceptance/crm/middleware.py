import time
from collections.abc import Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class Timing:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_wrapper(message: Message) -> None:
            await send(message)                                                        # truth-unresolved: external

        await self.app(scope, receive, send_wrapper)                                   # truth-unresolved: external


def measure(step: Callable[[], int]) -> int:
    return step()                                                                      # truth-unresolved: ambiguous


def quick() -> int:
    return 1


def blocking() -> int:
    time.sleep(1)
    return 2


async def both() -> int:
    return measure(quick) + measure(blocking)                                          # truth-absent: ASYNC_BLOCKING_CALL_INDIRECT


class Worker:
    def run(self) -> int:
        time.sleep(1)
        return 0


def schedule(job: Callable[[], int]) -> int:
    return job()                                                                       # truth-edge: crm.middleware.Worker.run


async def start(worker: Worker) -> int:
    return schedule(worker.run)                                                        # truth: ASYNC_BLOCKING_CALL_INDIRECT
