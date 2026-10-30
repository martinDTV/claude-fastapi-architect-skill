"""Defect-observing tests: each one fails on the defective service and passes after the recipe."""

import asyncio
import unittest
import warnings

from tests.fakes import FakeAsyncSession, stub_sqlalchemy_if_missing

stub_sqlalchemy_if_missing()

from shop import service  # noqa: E402


def run(coro):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return asyncio.run(coro)


class DefectTests(unittest.TestCase):
    def test_import_is_atomic_when_an_item_fails(self) -> None:
        session = FakeAsyncSession(fail_on_add_number=3)
        with self.assertRaises(ValueError):
            run(service.import_items(session, ["a", "b", "c"]))
        self.assertEqual(session.commits, 0, "a failure half-way must leave nothing committed")
        self.assertEqual(session.committed, [])

    def test_import_commits_everything_once_on_success(self) -> None:
        session = FakeAsyncSession()
        run(service.import_items(session, ["a", "b", "c"]))
        self.assertEqual(session.committed, ["a", "b", "c"])
        self.assertEqual(session.commits, 1)

    def test_reprice_commits_both_writes_without_error(self) -> None:
        session = FakeAsyncSession()
        run(service.reprice(session, "item", "adjustment"))
        self.assertEqual(session.committed, ["item", "adjustment"])

    def test_audit_trail_executes_the_statement(self) -> None:
        session = FakeAsyncSession()
        run(service.audit_trail(session, "insert into audit values (1)"))
        self.assertEqual(session.executed, ["insert into audit values (1)"])

    def test_summary_control_is_untouched(self) -> None:
        session = FakeAsyncSession()
        self.assertEqual(run(service.summary(session, ["a", "b"])), 2)
        self.assertEqual(session.commits, 1)


if __name__ == "__main__":
    unittest.main()
