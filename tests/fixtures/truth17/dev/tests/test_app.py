"""Test code: shared sessions, sync helpers and bare tasks are the test's own business."""

import asyncio

import requests

from app.services import accounts


async def test_gather_on_one_session(session) -> None:
    await asyncio.gather(accounts.compute_summary(session), accounts.compute_summary(session))  # truth: SA_SESSION_SHARED_CONCURRENTLY


def test_fetch() -> None:
    assert requests.get("https://x.invalid").status_code == 200                       # truth-absent: REL_HTTP_CALL_WITHOUT_TIMEOUT
