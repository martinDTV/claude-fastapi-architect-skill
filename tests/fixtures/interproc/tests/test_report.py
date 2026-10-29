import time

from app.helpers import compute_report


async def test_report(db):
    compute_report(db)                       # tests call sync helpers freely: not reported
    time.sleep(0.01)
