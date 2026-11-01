"""In-memory storage shared by the routers. Deliberately simple: dicts keyed by id."""

from itertools import count

ITEMS: dict[int, dict] = {}
USERS: dict[int, dict] = {}

_item_ids = count(1)
_user_ids = count(1)


def next_item_id() -> int:
    return next(_item_ids)


def next_user_id() -> int:
    return next(_user_ids)


def reset() -> None:   # used by the tests
    ITEMS.clear()
    USERS.clear()
