"""Transaction and concurrency rules (fixture transactions)."""

from __future__ import annotations

import unittest

from _audit_support import ProjectCase, audit_fixture, by_rule, lines_of


class TransactionsFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = audit_fixture("transactions")
        cls.grouped = by_rule(cls.report)

    def test_commit_inside_loop_reported_once_and_single_commit_not(self) -> None:
        items = self.grouped["TX_COMMIT_IN_LOOP"]
        self.assertEqual([(f["file"], f["line"], f["confidence"]) for f in items], [("app/service.py", 16, "high")])
        self.assertIn("loop at line 14", items[0]["message"])
        self.assertNotIn(22, lines_of(self.report, "TX_COMMIT_IN_LOOP"))  # commit_once

    def test_external_calls_inside_explicit_transaction(self) -> None:
        items = sorted(self.grouped["TX_EXTERNAL_CALL_IN_TRANSACTION"], key=lambda f: f["line"])
        self.assertEqual([(f["line"], f["confidence"]) for f in items], [(27, "high"), (28, "high"), (29, "medium")])
        self.assertIn("client.post()", items[0]["message"])
        self.assertIn("requests.get", items[1]["message"])
        self.assertIn("sleep", items[2]["message"])
        # http_outside_transaction: the POST after the block is fine
        self.assertNotIn(35, [f["line"] for f in items])

    def test_read_modify_write_without_lock_or_version(self) -> None:
        items = self.grouped["TX_READ_MODIFY_WRITE"]
        self.assertEqual([(f["file"], f["line"], f["confidence"], f["verification"]) for f in items],
                         [("app/service.py", 40, "low", "not_verifiable_statically")])
        self.assertIn("two sessions", items[0]["recommended_test"])
        self.assertIn("FOR UPDATE", items[0]["recommended_test"])
        # locked_update uses with_for_update; atomic_update writes an atomic UPDATE
        self.assertNotIn(47, lines_of(self.report, "TX_READ_MODIFY_WRITE"))

    def test_commit_failure_swallowed_without_rollback(self) -> None:
        items = self.grouped["TX_COMMIT_WITHOUT_ROLLBACK"]
        self.assertEqual([(f["file"], f["line"]) for f in items], [("app/service.py", 60)])
        # rollback_on_failure rolls back and re-raises; bounded_by_context_manager closes the session
        self.assertEqual(lines_of(self.report, "TX_COMMIT_WITHOUT_ROLLBACK"), [60])

    def test_blocking_calls_inside_the_transaction_are_also_async_findings(self) -> None:
        self.assertEqual(lines_of(self.report, "ASYNC_BLOCKING_CALL"), [28, 29])


class TransactionsInlineTests(ProjectCase):
    def test_commit_in_nested_loops_is_reported_once(self) -> None:
        self.write("app/s.py", """
            from sqlalchemy.ext.asyncio import AsyncSession

            async def run(session: AsyncSession, groups):
                for group in groups:
                    for item in group:
                        item.flag = True
                        await session.commit()
            """)
        self.assertEqual(len(self.findings("TX_COMMIT_IN_LOOP")), 1)

    def test_commit_on_unrelated_objects_is_not_reported(self) -> None:
        self.write("app/s.py", """
            import sqlalchemy

            def consume(consumer, messages):
                for message in messages:
                    consumer.commit()        # a Kafka consumer, not a session
            """)
        self.assertEqual(self.findings("TX_COMMIT_IN_LOOP"), [])

    def test_sync_session_begin_block_with_requests(self) -> None:
        self.write("app/s.py", """
            import requests
            from sqlalchemy.orm import Session

            def sync_flow(session: Session) -> None:
                with session.begin():
                    requests.post("https://x.invalid")
            """)
        found = self.findings("TX_EXTERNAL_CALL_IN_TRANSACTION")
        self.assertEqual([(f["line"], f["confidence"]) for f in found], [(6, "high")])

    def test_read_modify_write_via_query_first_and_explicit_assignment(self) -> None:
        self.write("app/s.py", """
            from sqlalchemy.orm import Session

            def bump(db: Session, item_id: int) -> None:
                item = db.query(Item).filter_by(id=item_id).first()
                item.count = item.count + 1
                db.commit()
            """)
        found = self.findings("TX_READ_MODIFY_WRITE")
        self.assertEqual([f["line"] for f in found], [5])
        self.assertEqual(found[0]["symbol"], "item.count")

    def test_plain_attribute_assignment_is_not_read_modify_write(self) -> None:
        self.write("app/s.py", """
            from sqlalchemy.ext.asyncio import AsyncSession

            async def rename(session: AsyncSession, item_id: int, name: str) -> None:
                item = await session.get(Item, item_id)
                item.name = name                 # overwrite, not derived from the old value
                await session.commit()
            """)
        self.assertEqual(self.findings("TX_READ_MODIFY_WRITE"), [])

    def test_rules_skip_test_environment_downgrade_and_migrations(self) -> None:
        self.write("tests/test_s.py", """
            from sqlalchemy.ext.asyncio import AsyncSession

            async def test_loop(session: AsyncSession, rows):
                for row in rows:
                    await session.commit()
            """)
        found = self.findings("TX_COMMIT_IN_LOOP")
        self.assertEqual([(f["environment"], f["confidence"]) for f in found], [("test", "medium")])


if __name__ == "__main__":
    unittest.main()
