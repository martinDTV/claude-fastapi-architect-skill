"""Throwaway PostgreSQL clusters for opt-in tests (v2.0): ``initdb`` + ``pg_ctl`` in a temporary directory.

Mirrors the bootstrap of ``examples/tenant-ledger/tests/conftest.py``: a cluster owned by the current user with
``trust`` authentication on 127.0.0.1 and a free port, ``fsync=off`` (disposable data), one database per request,
stopped and deleted afterwards. Nothing here touches an existing server: every cluster lives under a fresh
``tempfile.mkdtemp`` directory. The opt-in gate is the caller's (``FA_DESIGN_PG=1``); this module only finds
binaries and manages the lifecycle.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

PG_PREFIXES = (
    "/opt/homebrew/opt/postgresql@16/bin",
    "/opt/homebrew/opt/postgresql@17/bin",
    "/opt/homebrew/opt/postgresql@15/bin",
    "/usr/lib/postgresql/16/bin",
    "/usr/lib/postgresql/17/bin",
    "/usr/local/pgsql/bin",
)
OWNER = "fa_owner"


def find_pg(binary: str) -> Optional[str]:
    found = shutil.which(binary)
    if found:
        return found
    for prefix in PG_PREFIXES:
        candidate = os.path.join(prefix, binary)
        if os.path.exists(candidate):
            return candidate
    return None


def pg_available() -> bool:
    return all(find_pg(b) for b in ("initdb", "pg_ctl", "psql"))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class ThrowawayCluster:
    """``with ThrowawayCluster() as pg: url = pg.create_database("x")``; stopped and deleted on exit."""

    tmp: str = ""
    port: int = 0
    log: List[str] = field(default_factory=list)  # commands run, for evidence
    _env: Dict[str, str] = field(default_factory=dict)

    def __enter__(self) -> "ThrowawayCluster":
        initdb, pg_ctl = find_pg("initdb"), find_pg("pg_ctl")
        if not (initdb and pg_ctl and find_pg("psql")):
            raise RuntimeError("PostgreSQL binaries (initdb, pg_ctl, psql) not found")
        self.tmp = tempfile.mkdtemp(prefix="fa-design-pg-")
        data, sockets = os.path.join(self.tmp, "data"), os.path.join(self.tmp, "sock")
        os.makedirs(sockets)
        self.port = free_port()
        self._env = dict(os.environ, LC_ALL="C", TZ="UTC")
        self._run([initdb, "-D", data, "-U", OWNER, "-A", "trust", "--no-sync"])
        self._run([pg_ctl, "-D", data, "-l", os.path.join(self.tmp, "pg.log"), "-w", "-t", "30", "-o", f"-p {self.port} -k {sockets} -h 127.0.0.1 -c fsync=off -c synchronous_commit=off -c max_connections=50", "start"])
        return self

    def __exit__(self, *exc) -> None:
        pg_ctl = find_pg("pg_ctl")
        if pg_ctl and self.tmp:
            subprocess.run([pg_ctl, "-D", os.path.join(self.tmp, "data"), "-m", "immediate", "-w", "stop"], capture_output=True, env=self._env)
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv: List[str]) -> subprocess.CompletedProcess:
        self.log.append(" ".join(argv))
        return subprocess.run(argv, check=True, capture_output=True, text=True, env=self._env)

    def psql(self, sql: str, db: str = "postgres") -> str:
        psql = find_pg("psql")
        assert psql
        proc = self._run([psql, "-h", "127.0.0.1", "-p", str(self.port), "-U", OWNER, "-d", db, "-v", "ON_ERROR_STOP=1", "-At", "-c", sql])
        return proc.stdout.strip()

    def create_database(self, name: str) -> str:
        self.psql(f"CREATE DATABASE {name}")
        return f"postgresql+asyncpg://{OWNER}@127.0.0.1:{self.port}/{name}"

    def server_version(self) -> str:
        return self.psql("SHOW server_version")
