"""Runtime reproductions of properties the static auditor can only suspect.

These tests need a real PostgreSQL. They create a throwaway cluster with
``initdb``/``pg_ctl`` in a temporary directory (never an existing database),
run on a free local port, and remove it afterwards. They are skipped when
``pg_ctl`` or a psycopg driver is not available, or when ``AUDIT_SKIP_PG=1``.

Properties demonstrated:
1. TX_READ_MODIFY_WRITE: two READ COMMITTED transactions lose an update; SELECT ... FOR UPDATE prevents it.
2. SEC_RLS_SET_NOT_LOCAL: a session-level SET survives the transaction on the same connection; SET LOCAL does not.
3. MIG_NOT_NULL_WITHOUT_DEFAULT: ADD COLUMN ... NOT NULL without a default fails on a table with rows.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

try:  # either driver works; the tests use only DB-API features
    import psycopg as _driver  # type: ignore
    _CONNECT = _driver.connect
    _DRIVER = "psycopg"
except ImportError:  # pragma: no cover - depends on the machine
    try:
        import psycopg2 as _driver  # type: ignore
        _CONNECT = _driver.connect
        _DRIVER = "psycopg2"
    except ImportError:
        _CONNECT = None
        _DRIVER = None


def _find(binary: str):
    found = shutil.which(binary)
    if found:
        return found
    for prefix in ("/opt/homebrew/opt/postgresql@16/bin", "/opt/homebrew/opt/postgresql@17/bin", "/opt/homebrew/opt/postgresql@15/bin", "/usr/lib/postgresql/16/bin", "/usr/local/pgsql/bin"):
        candidate = os.path.join(prefix, binary)
        if os.path.exists(candidate):
            return candidate
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


SKIP_REASON = None
if os.environ.get("AUDIT_SKIP_PG") == "1":
    SKIP_REASON = "AUDIT_SKIP_PG=1"
elif _CONNECT is None:
    SKIP_REASON = "no psycopg driver installed"
elif not (_find("initdb") and _find("pg_ctl")):
    SKIP_REASON = "initdb/pg_ctl not found"


@unittest.skipIf(SKIP_REASON is not None, SKIP_REASON or "")
class EphemeralPostgresTests(unittest.TestCase):
    """One throwaway cluster per test class; every test gets its own database objects."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="fa-pg-")
        cls.data = os.path.join(cls.tmp, "data")
        cls.sockets = os.path.join(cls.tmp, "sock")
        os.makedirs(cls.sockets)
        cls.port = _free_port()
        env = dict(os.environ, LC_ALL="C", TZ="UTC")
        subprocess.run([_find("initdb"), "-D", cls.data, "-U", "audit", "-A", "trust", "--no-sync"], check=True, capture_output=True, env=env)
        cls.log = os.path.join(cls.tmp, "pg.log")
        subprocess.run(
            [_find("pg_ctl"), "-D", cls.data, "-l", cls.log, "-w", "-t", "30", "-o", f"-p {cls.port} -k {cls.sockets} -h 127.0.0.1 -c fsync=off -c synchronous_commit=off", "start"],
            check=True, capture_output=True, env=env,
        )
        cls.dsn = f"host=127.0.0.1 port={cls.port} user=audit dbname=postgres"
        deadline = time.monotonic() + 30
        while True:
            try:
                _CONNECT(cls.dsn).close()
                break
            except Exception:  # noqa: BLE001 - server still starting
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run([_find("pg_ctl"), "-D", cls.data, "-m", "immediate", "-w", "stop"], capture_output=True)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def connect(self):
        conn = _CONNECT(self.dsn)
        conn.autocommit = False
        self.addCleanup(conn.close)
        return conn

    def setup_table(self, ddl: str) -> None:
        conn = self.connect()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.close()

    # ------------------------------------------------------------------ 1 lost update
    def test_read_modify_write_loses_an_update_and_for_update_prevents_it(self) -> None:
        self.setup_table("DROP TABLE IF EXISTS accounts; CREATE TABLE accounts (id int PRIMARY KEY, balance int NOT NULL); INSERT INTO accounts VALUES (1, 100);")

        def read_modify_write(lock: bool) -> int:
            a, b = self.connect(), self.connect()
            for_update = " FOR UPDATE" if lock else ""
            with a.cursor() as ca, b.cursor() as cb:
                ca.execute("SELECT balance FROM accounts WHERE id = 1" + for_update)
                (balance_a,) = ca.fetchone()
                ca.execute("UPDATE accounts SET balance = %s WHERE id = 1", (balance_a + 10,))
                if lock:
                    # b blocks on the row lock until a commits; use a timeout to prove it waits
                    cb.execute("SET LOCAL lock_timeout = '300ms'")
                    with self.assertRaises(Exception):
                        cb.execute("SELECT balance FROM accounts WHERE id = 1 FOR UPDATE")
                    b.rollback()
                    a.commit()
                    cb.execute("SELECT balance FROM accounts WHERE id = 1 FOR UPDATE")
                    (balance_b,) = cb.fetchone()
                    cb.execute("UPDATE accounts SET balance = %s WHERE id = 1", (balance_b + 10,))
                    b.commit()
                else:
                    cb.execute("SELECT balance FROM accounts WHERE id = 1")
                    (balance_b,) = cb.fetchone()
                    a.commit()
                    cb.execute("UPDATE accounts SET balance = %s WHERE id = 1", (balance_b + 10,))
                    b.commit()
            check = self.connect()
            with check.cursor() as cur:
                cur.execute("SELECT balance FROM accounts WHERE id = 1")
                (final,) = cur.fetchone()
            check.rollback()
            return final

        self.assertEqual(read_modify_write(lock=False), 110, "second writer overwrote the first: one update lost")
        self.setup_table("UPDATE accounts SET balance = 100 WHERE id = 1;")
        self.assertEqual(read_modify_write(lock=True), 120, "FOR UPDATE serialises the writers: both updates kept")

    # ------------------------------------------------------------------ 2 SET vs SET LOCAL
    def test_session_level_set_leaks_across_transactions_on_the_same_connection(self) -> None:
        conn = self.connect()
        with conn.cursor() as cur:
            cur.execute("SET app.tenant_id = 'tenant-a'")
            conn.commit()
            cur.execute("SELECT current_setting('app.tenant_id', true)")
            self.assertEqual(cur.fetchone()[0], "tenant-a", "session-level SET persists after commit: the next request on this pooled connection inherits it")
            conn.rollback()
            cur.execute("SET LOCAL app.tenant_id = 'tenant-b'")
            cur.execute("SELECT current_setting('app.tenant_id', true)")
            self.assertEqual(cur.fetchone()[0], "tenant-b")
            conn.commit()
            cur.execute("SELECT current_setting('app.tenant_id', true)")
            self.assertEqual(cur.fetchone()[0], "tenant-a", "SET LOCAL ends with the transaction; the earlier session value is what remains")
            conn.rollback()
            cur.execute("SELECT set_config('app.tenant_id', 'tenant-c', true)")
            conn.commit()
            cur.execute("SELECT current_setting('app.tenant_id', true)")
            self.assertEqual(cur.fetchone()[0], "tenant-a", "set_config(..., true) is transaction-local")
            conn.rollback()

    # ------------------------------------------------------------------ 3 NOT NULL without default
    def test_add_not_null_column_without_default_fails_on_populated_table(self) -> None:
        self.setup_table("DROP TABLE IF EXISTS orders; CREATE TABLE orders (id int PRIMARY KEY); INSERT INTO orders VALUES (1);")
        conn = self.connect()
        with conn.cursor() as cur:
            with self.assertRaises(Exception) as ctx:
                cur.execute("ALTER TABLE orders ADD COLUMN region text NOT NULL")
            self.assertIn("null", str(ctx.exception).lower())
            conn.rollback()
            cur.execute("ALTER TABLE orders ADD COLUMN region text")  # expand
            cur.execute("UPDATE orders SET region = 'EU' WHERE region IS NULL")  # backfill
            cur.execute("ALTER TABLE orders ALTER COLUMN region SET NOT NULL")  # contract
            conn.commit()


if __name__ == "__main__":
    unittest.main()
