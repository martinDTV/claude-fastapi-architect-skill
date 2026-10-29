"""Test code: helpers call sync functions freely; cross-function async rules stay quiet here."""

import asyncio

from app.modules.accounts.callbacks import run_with, slow


async def test_blocking_helper_in_tests() -> None:
    assert run_with(slow, 1) == 1                                                      # truth-absent: ASYNC_BLOCKING_CALL_INDIRECT


async def test_fire() -> None:
    asyncio.create_task(asyncio.sleep(0))                                              # truth-absent: ASYNC_TASK_REFERENCE_DROPPED
