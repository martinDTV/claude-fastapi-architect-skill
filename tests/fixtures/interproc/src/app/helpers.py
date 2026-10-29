import time


def slow_total() -> int:
    time.sleep(1)                           # blocking, two calls below the route
    return 1


def compute_report(session) -> int:
    rows = session.execute("select 1")      # untyped here; typed through the caller
    return slow_total()


def pure(x: int) -> int:
    return x * 2
