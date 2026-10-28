"""Alembic migration rules (fixture alembic_project)."""

from __future__ import annotations

import unittest

from _audit_support import ProjectCase, audit_fixture, by_rule, lines_of

DESTRUCTIVE = "alembic/versions/0001_destructive.py"
EXPAND = "alembic/versions/0002_expand.py"
CONTRACT = "alembic/versions/0003_contract.py"


class AlembicFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = audit_fixture("alembic_project")
        cls.grouped = by_rule(cls.report)

    def test_destructive_operations_are_reported_as_review_items(self) -> None:
        self.assertEqual(lines_of(self.report, "MIG_DROP_TABLE", DESTRUCTIVE), [10])
        self.assertEqual(lines_of(self.report, "MIG_DROP_COLUMN", DESTRUCTIVE), [11])
        self.assertEqual(lines_of(self.report, "MIG_TYPE_CHANGE", DESTRUCTIVE), [12])
        drop_table = self.grouped["MIG_DROP_TABLE"][0]
        self.assertEqual((drop_table["severity"], drop_table["environment"], drop_table["verification"]),
                         ("high", "migration", "needs_runtime_check"))
        self.assertIn("legacy_events", drop_table["message"])
        self.assertIn("expand-contract", drop_table["message"])

    def test_not_null_without_default_on_add_and_alter(self) -> None:
        items = sorted(self.grouped["MIG_NOT_NULL_WITHOUT_DEFAULT"], key=lambda f: (f["file"], f["line"]))
        self.assertEqual([(f["file"], f["line"], f["confidence"]) for f in items],
                         [(DESTRUCTIVE, 13, "high"), (DESTRUCTIVE, 14, "medium"), (CONTRACT, 11, "low")])
        self.assertIn("backfill", items[2]["message"].lower())
        # add_column with server_default (0002 line 20) is not reported
        self.assertEqual(lines_of(self.report, "MIG_NOT_NULL_WITHOUT_DEFAULT", EXPAND), [])

    def test_index_on_existing_table_without_concurrently(self) -> None:
        self.assertEqual(lines_of(self.report, "MIG_INDEX_NOT_CONCURRENT", DESTRUCTIVE), [15])
        # 0002: index on the table created in the same migration, and a CONCURRENTLY index, are silent
        self.assertEqual(lines_of(self.report, "MIG_INDEX_NOT_CONCURRENT", EXPAND), [])

    def test_raw_sql_classification(self) -> None:
        items = sorted(self.grouped["MIG_RAW_SQL"], key=lambda f: (f["file"], f["line"]))
        self.assertEqual([(f["file"], f["line"], f["severity"]) for f in items],
                         [(DESTRUCTIVE, 16, "low"), (DESTRUCTIVE, 19, "medium"), (CONTRACT, 12, "info")])
        self.assertIn("ALTER TABLE", items[0]["message"])
        self.assertIn("DROP TABLE", items[1]["message"])
        self.assertIn("dynamically built", items[2]["message"])
        # ENABLE ROW LEVEL SECURITY, CREATE POLICY and DROP FUNCTION IF EXISTS in 0002 are routine
        self.assertEqual(lines_of(self.report, "MIG_RAW_SQL", EXPAND), [])

    def test_ddl_mixed_with_dml_but_seeds_of_new_tables_are_fine(self) -> None:
        self.assertEqual(lines_of(self.report, "MIG_DDL_WITH_DML", DESTRUCTIVE), [17])
        self.assertEqual(lines_of(self.report, "MIG_DDL_WITH_DML", CONTRACT), [10])
        # 0002 seeds the `regions` table it creates (bulk_insert through the create_table variable
        # and a raw INSERT through an aliased op): no finding
        self.assertEqual(lines_of(self.report, "MIG_DDL_WITH_DML", EXPAND), [])

    def test_drop_constraint_replacement_pattern_is_low_severity(self) -> None:
        items = sorted(self.grouped["MIG_DROP_CONSTRAINT"], key=lambda f: (f["file"], f["line"]))
        self.assertEqual([(f["file"], f["line"], f["severity"]) for f in items],
                         [(DESTRUCTIVE, 18, "medium"), (EXPAND, 25, "low")])
        self.assertIn("replacement pattern", items[1]["message"])
        self.assertIn("on 'orders'", items[0]["message"])

    def test_downgrade_noop_and_raise(self) -> None:
        items = sorted(self.grouped["MIG_DOWNGRADE_NOOP"], key=lambda f: f["file"])
        self.assertEqual([(f["file"], f["line"]) for f in items], [(DESTRUCTIVE, 22), (CONTRACT, 15)])
        self.assertIn("is empty", items[0]["message"])
        self.assertIn("raises", items[1]["message"])
        self.assertEqual(lines_of(self.report, "MIG_DOWNGRADE_NOOP", EXPAND), [])

    def test_env_py_is_migration_environment_without_sqlalchemy_findings(self) -> None:
        env_findings = [f for f in self.report["findings"] if f["file"] == "alembic/env.py"]
        self.assertEqual(env_findings, [])
        self.assertEqual(self.report["summary"]["by_environment"]["migration"], 16)

    def test_migration_category_only(self) -> None:
        from _audit_support import audit

        report = audit_fixture("alembic_project", options=audit.AuditOptions(categories={"migrations"}))
        self.assertTrue(all(f["rule"].startswith("MIG_") for f in report["findings"]))
        self.assertEqual(report["summary"]["by_category"]["migrations"], report["summary"]["findings_total"])


class AlembicInlineTests(ProjectCase):
    def test_migration_detected_by_content_outside_versions_directory(self) -> None:
        self.write("db/rev_abc.py", """
            from alembic import op

            def upgrade():
                op.drop_table("x")

            def downgrade():
                pass
            """)
        found = self.findings("MIG_DROP_TABLE")
        self.assertEqual([(f["line"], f["environment"]) for f in found], [(4, "migration")])

    def test_unresolved_op_name_is_reported_with_medium_confidence(self) -> None:
        self.write("migrations/versions/001_x.py", """
            from alembic import *   # op comes from a star import

            def upgrade():
                op.drop_column("t", "c")
            """)
        found = self.findings("MIG_DROP_COLUMN")
        self.assertEqual([(f["line"], f["confidence"]) for f in found], [(4, "medium")])

    def test_raw_create_index_without_concurrently_on_existing_table(self) -> None:
        self.write("migrations/versions/002_x.py", """
            from alembic import op
            import sqlalchemy as sa

            def upgrade():
                op.execute(sa.text("CREATE INDEX ix_a ON orders (a)"))
                op.execute("CREATE INDEX CONCURRENTLY ix_b ON orders (b)")
                op.execute("CREATE UNIQUE INDEX ix_c ON fresh (c)")

            def downgrade():
                op.drop_index("ix_a")
            """)
        found = self.findings("MIG_INDEX_NOT_CONCURRENT")
        self.assertEqual([f["line"] for f in found], [5, 7])

    def test_non_migration_module_defining_upgrade_is_not_a_migration(self) -> None:
        self.write("app/upgrade_tool.py", """
            def upgrade():
                return "drop_table"
            """)
        self.assertEqual(self.rules(), [])
        self.assertEqual(self.run_audit()["summary"]["by_environment"]["migration"], 0)


if __name__ == "__main__":
    unittest.main()
