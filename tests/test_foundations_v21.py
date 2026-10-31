"""v2.1, increment 0: engineering foundations.

* ``fastapi_common.artefacts``: one definition of the transient artefacts (tool caches, bytecode, Finder droppings,
  virtual environments) shared by the audit walker, the verification workspace copy and every tree comparison; a
  snapshot comparison must not depend on whether a linter or a file manager touched the directory;
* ``fastapi_common.fs``: the atomic write, durable on request, and what the journal does with it: every write of the
  recovery protocol is fsync'ed (file before the rename, directory entry after it); a failed sync of a file is an
  error that leaves a recoverable journal, never a silent downgrade;
* the plugin version is declared once.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _audit_support import REPO_ROOT, SKILL_SCRIPTS

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

import fastapi_design as design  # noqa: E402
from fastapi_audit.discovery import DEFAULT_EXCLUDED_DIRS  # noqa: E402
from fastapi_audit.model import TOOL_VERSION as AUDIT_TOOL_VERSION  # noqa: E402
from fastapi_audit.remediate.isolation import DEFAULT_IGNORED_DIRS  # noqa: E402
from fastapi_common import artefacts, fs  # noqa: E402
from fastapi_common.version import TOOL_VERSION  # noqa: E402
from fastapi_design import journal  # noqa: E402
from fastapi_design.generator import WriteError  # noqa: E402

SNAPSHOT = REPO_ROOT / "plugins" / "fastapi-architect" / "examples" / "generated" / "inventory-service"


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fa-foundations-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()


# --------------------------------------------------------------------------- #
# transient artefacts
# --------------------------------------------------------------------------- #


class ArtefactTests(TempCase):
    def _tree(self) -> Path:
        root = self.tmp / "project"
        for rel, data in {
            "pkg/a.py": b"x = 1\n",
            "pkg/__pycache__/a.cpython-313.pyc": b"\x8f\x00binary",
            ".ruff_cache/0.17.3/16292743293045692340": b"\x8f\x91\x90",
            ".ruff_cache/CACHEDIR.TAG": b"Signature: 8a477f597d28d172789f06886806bc55",
            ".pytest_cache/v/cache/nodeids": b"[]",
            ".DS_Store": b"\x00\x00\x00\x01Bud1",
            "pkg/Thumbs.db": b"\xd0\xcf",
            "stray.pyc": b"\x00",
            ".venv/lib/python3.13/site-packages/x.py": b"",
            "env/keep.py": b"# a package named env is content\n",
            ".github/workflows/ci.yml": b"name: ci\n",
            "notes.txt": b"kept\n",
        }.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return root

    def test_walk_files_skips_transient_artefacts_and_keeps_everything_else(self) -> None:
        root = self._tree()
        self.assertEqual(artefacts.relative_files(root), [".github/workflows/ci.yml", "env/keep.py", "notes.txt", "pkg/a.py"])
        self.assertEqual([p.name for p in artefacts.walk_files(root)], ["ci.yml", "keep.py", "notes.txt", "a.py"])

    def test_is_transient_classifies_paths_by_their_parts(self) -> None:
        self.assertTrue(artefacts.is_transient([".ruff_cache", "0.17.3", "x"]))
        self.assertTrue(artefacts.is_transient(["pkg", "__pycache__", "a.pyc"]))
        self.assertTrue(artefacts.is_transient([".DS_Store"]))
        self.assertTrue(artefacts.is_transient(["pkg", "stray.pyc"]))
        self.assertFalse(artefacts.is_transient(["env", "keep.py"]))
        self.assertFalse(artefacts.is_transient([".github", "workflows", "ci.yml"]))
        self.assertFalse(artefacts.is_transient(["src", "app", "main.py.fa-tmp"]), "an interrupted write is evidence, not noise")
        self.assertFalse(artefacts.is_transient([]))

    def test_the_consumers_share_the_transient_set(self) -> None:
        transient = set(artefacts.TRANSIENT_DIRS)
        self.assertTrue(transient <= set(DEFAULT_EXCLUDED_DIRS), transient - set(DEFAULT_EXCLUDED_DIRS))
        self.assertTrue(transient <= set(DEFAULT_IGNORED_DIRS), transient - set(DEFAULT_IGNORED_DIRS))
        self.assertTrue(set(artefacts.VIRTUALENV_DIRS) <= set(DEFAULT_EXCLUDED_DIRS) & set(DEFAULT_IGNORED_DIRS))
        self.assertNotIn("env", DEFAULT_EXCLUDED_DIRS, "the audit scans a directory named env: it is a plausible package name")
        self.assertIn("env", DEFAULT_IGNORED_DIRS, "the workspace copy never carries a virtual environment named env (v1.9 behaviour)")
        self.assertIn("site-packages", DEFAULT_EXCLUDED_DIRS)
        self.assertNotIn(".github", artefacts.TRANSIENT_DIRS)
        self.assertNotIn(".vscode", artefacts.TRANSIENT_DIRS)

    def test_gitignore_keeps_transient_directories_out_of_the_repository(self) -> None:
        ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        for name in ("__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache", ".hypothesis"):
            self.assertIn(f"{name}/", ignored, name)
        for name in (".DS_Store", "Thumbs.db"):
            self.assertIn(name, ignored, name)

    def test_a_snapshot_comparison_ignores_artefacts_left_by_tools(self) -> None:
        copy = self.tmp / "inventory-service"
        shutil.copytree(SNAPSHOT, copy, ignore=shutil.ignore_patterns(*artefacts.DEFAULT_WALK_SKIP))
        (copy / ".ruff_cache" / "0.17.3").mkdir(parents=True)
        (copy / ".ruff_cache" / "0.17.3" / "16292743293045692340").write_bytes(b"\x8f\x91\x90")
        (copy / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
        (copy / "src" / "inventory_service" / "__pycache__").mkdir(parents=True)
        (copy / "src" / "inventory_service" / "__pycache__" / "main.cpython-313.pyc").write_bytes(b"\x00")
        self.assertEqual(artefacts.relative_files(copy), artefacts.relative_files(SNAPSHOT))
        for p in artefacts.walk_files(copy):
            p.read_text(encoding="utf-8")  # every file a comparison reads is text
        self.assertEqual(set(design.verify_manifest(str(copy))["status"].values()), {"identical"})


# --------------------------------------------------------------------------- #
# filesystem helpers and durability
# --------------------------------------------------------------------------- #


class FsHelperTests(TempCase):
    def test_digests_and_contract_relative_paths(self) -> None:
        self.assertEqual(fs.sha256_text("abc"), fs.sha256_bytes(b"abc"))
        path = self.tmp / "f.txt"
        path.write_text("abc", encoding="utf-8")
        self.assertEqual(fs.sha256_path(path), fs.sha256_text("abc"))
        self.assertIsNone(fs.sha256_path(self.tmp))
        self.assertEqual(fs.resolve_against("project", str(self.tmp / "contracts" / "evolve.json")), self.tmp / "contracts" / "project")
        self.assertEqual(fs.resolve_against(str(self.tmp / "abs"), str(self.tmp / "contracts" / "evolve.json")), self.tmp / "abs")
        self.assertEqual(fs.resolve_against("relative", None), Path("relative"))

    @unittest.skipUnless(os.name == "posix", "symlinks")
    def test_symlink_on_path_names_the_offending_component(self) -> None:
        real = self.tmp / "real"
        real.mkdir()
        (self.tmp / "link").symlink_to(real, target_is_directory=True)
        self.assertEqual(fs.symlink_on_path(self.tmp, "link/file.txt"), self.tmp / "link")
        self.assertIsNone(fs.symlink_on_path(self.tmp, "real/file.txt"))


class DurabilityTests(TempCase):
    def test_a_durable_write_syncs_the_file_before_the_rename_and_the_directory_after_it(self) -> None:
        target = self.tmp / "dir" / "file.txt"
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            events.append(("fsync", os.fstat(fd).st_mode & 0o170000 == 0o040000))  # True when the fd is a directory
            real_fsync(fd)

        def replace(src, dst):
            events.append(("replace", None))
            real_replace(src, dst)

        with mock.patch("os.fsync", side_effect=fsync), mock.patch("os.replace", side_effect=replace):
            synced = fs.atomic_write(target, b"new", durable=True)
        self.assertEqual(target.read_bytes(), b"new")
        self.assertEqual([e[0] for e in events], ["fsync", "replace", "fsync"])
        self.assertEqual([e[1] for e in events if e[0] == "fsync"], [False, True], "file first, then the directory entry")
        if os.name == "posix":
            self.assertTrue(synced)
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["file.txt"], "no temporary left")

    def test_a_non_durable_write_never_syncs(self) -> None:
        target = self.tmp / "file.txt"
        with mock.patch("os.fsync") as fsync:
            self.assertFalse(fs.atomic_write(target, b"x", durable=False))
        fsync.assert_not_called()
        self.assertEqual(target.read_bytes(), b"x")

    def test_a_failed_file_sync_is_an_error_that_leaves_the_destination_untouched(self) -> None:
        target = self.tmp / "file.txt"
        target.write_bytes(b"old")
        with mock.patch("os.fsync", side_effect=OSError(errno.EIO, "disk error")):
            with self.assertRaises(OSError):
                fs.atomic_write(target, b"new", durable=True)
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["file.txt"], "the temporary sibling is removed")

    @unittest.skipUnless(os.name == "posix", "directory sync is a POSIX concept")
    def test_a_filesystem_that_cannot_sync_directories_is_reported_not_fatal(self) -> None:
        target = self.tmp / "file.txt"
        real_fsync = os.fsync

        def fsync(fd):
            if os.fstat(fd).st_mode & 0o170000 == 0o040000:
                raise OSError(errno.EINVAL, "directory sync unsupported")
            real_fsync(fd)

        with mock.patch("os.fsync", side_effect=fsync):
            self.assertFalse(fs.atomic_write(target, b"new", durable=True))
        self.assertEqual(target.read_bytes(), b"new")

    def _project(self) -> Path:
        project = self.tmp / "project"
        project.mkdir()
        (project / "a.txt").write_text("a0\n", encoding="utf-8")
        (project / "b.txt").write_text("b0\n", encoding="utf-8")
        return project

    def test_the_journal_syncs_every_write_by_default_and_none_for_a_throwaway_copy(self) -> None:
        project = self._project()
        files = {"a.txt": "a1\n", "b.txt": "b1\n", "c.txt": "c1\n"}
        expected = {"a.txt": fs.sha256_text("a0\n"), "b.txt": fs.sha256_text("b0\n"), "c.txt": None}
        real_fsync = os.fsync
        calls = []
        with mock.patch("os.fsync", side_effect=lambda fd: (calls.append(fd), real_fsync(fd))):
            op = journal.write_recoverable(project, files, expected, kind="test", description="durable")
        self.assertEqual(op.state, "published")
        # three staged files, two backups, three landed files and four journal states, each a file sync plus a
        # directory sync; the exact count is an implementation detail, the lower bound is the guarantee
        self.assertGreaterEqual(len(calls), 2 * (3 + 2 + 3 + 4))
        self.assertEqual((project / "c.txt").read_text(encoding="utf-8"), "c1\n")
        self.assertEqual(journal.pending_operations(project), [])
        copy = self._project_copy(project)
        with mock.patch("os.fsync") as fsync:
            journal.write_recoverable(copy, {"a.txt": "a2\n"}, {"a.txt": fs.sha256_text("a1\n")}, kind="test", description="copy", durable=False)
        fsync.assert_not_called()

    def _project_copy(self, project: Path) -> Path:
        copy = self.tmp / "copy"
        shutil.copytree(project, copy)
        return copy

    def test_a_sync_failure_while_staging_leaves_no_staging_behind(self) -> None:
        project = self._project()
        with mock.patch("os.fsync", side_effect=OSError(errno.EIO, "disk error")):
            with self.assertRaises(WriteError) as ctx:
                journal.prepare(project, {"a.txt": "a1\n"}, {"a.txt": fs.sha256_text("a0\n")}, kind="test", description="staging")
        self.assertIn("could not stage", str(ctx.exception))
        self.assertFalse((project / ".fastapi-architect").exists())
        self.assertEqual((project / "a.txt").read_text(encoding="utf-8"), "a0\n")

    def test_a_sync_failure_while_publishing_leaves_a_recoverable_journal(self) -> None:
        project = self._project()
        files = {"a.txt": "a1\n", "b.txt": "b1\n"}
        expected = {"a.txt": fs.sha256_text("a0\n"), "b.txt": fs.sha256_text("b0\n")}
        op = journal.prepare(project, files, expected, kind="test", description="publishing")
        real_fsync = os.fsync
        state = {"landed": 0}

        def failing_fsync(fd):
            # the first landed file syncs; the sync of the second file's content fails
            if os.fstat(fd).st_mode & 0o170000 != 0o040000:
                state["landed"] += 1
                if state["landed"] == 3:  # journal state 'publishing', file a, then file b
                    raise OSError(errno.EIO, "disk error")
            real_fsync(fd)

        with mock.patch("os.fsync", side_effect=failing_fsync):
            with self.assertRaises(OSError):
                journal.publish(project, op)
        pending = journal.pending_operations(project)
        self.assertEqual([p.state for p in pending], ["publishing"])
        self.assertEqual(journal.classify(project, pending[0]), {"a.txt": "published", "b.txt": "pending"})
        report = journal.recover(project, pending[0], strategy="rollback")
        self.assertEqual(report.state, "rolled_back")
        self.assertEqual((project / "a.txt").read_text(encoding="utf-8"), "a0\n")
        self.assertEqual((project / "b.txt").read_text(encoding="utf-8"), "b0\n")
        self.assertEqual(journal.pending_operations(project), [])
        self.assertFalse((project / "b.txt.fa-tmp").exists())


# --------------------------------------------------------------------------- #
# lint and version
# --------------------------------------------------------------------------- #


class LintTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ruff") or shutil.which("uvx"), "ruff is not available")
    def test_the_plugin_code_is_clean_under_the_committed_ruff_configuration(self) -> None:
        """ruff.toml at the repository root: py39 target, no pyupgrade rewrites, fixtures and examples excluded."""
        ruff = [shutil.which("ruff")] if shutil.which("ruff") else [shutil.which("uvx"), "ruff"]
        targets = [str(SKILL_SCRIPTS), str(REPO_ROOT / "scripts"), str(REPO_ROOT / "tests")]
        proc = subprocess.run([*ruff, "check", "--no-cache", "--config", str(REPO_ROOT / "ruff.toml"), *targets], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stdout[-4000:] + proc.stderr[-2000:])


class VersionTests(unittest.TestCase):
    def test_the_version_is_declared_once_and_matches_the_plugin_manifest(self) -> None:
        manifest = json.loads((REPO_ROOT / "plugins" / "fastapi-architect" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(TOOL_VERSION, manifest["version"])
        self.assertIs(AUDIT_TOOL_VERSION, TOOL_VERSION)


if __name__ == "__main__":
    unittest.main()
