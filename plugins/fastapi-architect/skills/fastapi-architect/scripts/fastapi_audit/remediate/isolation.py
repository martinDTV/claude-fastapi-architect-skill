"""Isolated copy of a project and the only way commands are ever executed by the remediation tool (v1.9).

Guarantees, each enforced in code and tested:

* the audited project is never written to: patches are applied to a copy under a temporary directory
  (``.git``, virtual environments, caches and build output are not copied), and the pipeline compares a
  digest of the project's Python files before and after the run;
* nothing executes unless the caller authorizes it explicitly (``ExecutionPolicy.authorized``); the command is
  split without a shell (arguments are passed verbatim, so ``python -c "a; b"`` is one argument and no shell
  ever interprets it), the program token may not contain shell metacharacters, and only interpreter/test-runner
  names are allowed (``python``, ``python3``, ``python3.x``, ``pytest``, ``uv``), extensible by the caller;
* the command runs with the copy as working directory, a fresh ``HOME`` inside the copy, an environment reduced
  to an explicit pass-through list (``PATH``, locale, ``TMPDIR`` by default: no tokens, no credentials), no
  stdin, a wall-clock timeout, and CPU-time and file-size limits on POSIX.

Not provided (documented, see ``references/safe-refactoring.md``): network isolation and filesystem
confinement beyond the fresh HOME and the digest check. Run the tool inside a container when the project's
tests may reach the network or write outside their tree.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .model import RemediationError, TestRun

DEFAULT_IGNORED_DIRS = (".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".nox", "dist", "build", ".eggs")
DEFAULT_ENV_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")
DEFAULT_ALLOWED_COMMANDS = ("python", "pytest", "uv")
INTERPRETER_RE = re.compile(r"^python(\d+(\.\d+)?)?$")
SHELL_TOKENS = (";", "&&", "||", "|", ">", "<", "`", "$(", "\n")
TAIL = 4000


@dataclass
class ExecutionPolicy:
    authorized: bool = False
    command: Optional[str] = None
    timeout_s: float = 600.0
    env_passthrough: Tuple[str, ...] = DEFAULT_ENV_PASSTHROUGH
    allowed_commands: Tuple[str, ...] = DEFAULT_ALLOWED_COMMANDS
    cpu_seconds: Optional[int] = None  # default: 2 x timeout
    max_file_bytes: int = 512 * 1024 * 1024

    def to_dict(self) -> Dict[str, object]:
        return {
            "execution": bool(self.authorized),
            "test_command": self.command,
            "timeout_s": self.timeout_s,
            "env_passthrough": list(self.env_passthrough),
            "allowed_commands": list(self.allowed_commands),
            "cpu_seconds": self.cpu_seconds or int(self.timeout_s * 2),
            "max_file_bytes": self.max_file_bytes,
        }


def command_allowed(argv: Sequence[str], allowed: Iterable[str]) -> Optional[str]:
    """None when the command may run; otherwise the reason it is refused."""
    if not argv:
        return "empty command"
    if any(bad in argv[0] for bad in SHELL_TOKENS):
        return f"shell metacharacters are not allowed in the program: {argv[0]!r}"
    name = os.path.basename(argv[0])
    allowed_set = set(allowed)
    if name in allowed_set or (INTERPRETER_RE.match(name) and "python" in allowed_set):
        return None
    return f"command {name!r} is not in the allowed list ({', '.join(sorted(allowed_set))}; interpreters match python, python3, python3.x)"


def parse_command(command: str) -> List[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError as exc:
        raise RemediationError(f"cannot parse the test command: {exc}")


@dataclass
class Workspace:
    root: Path  # temporary directory owning the copy
    project: Path  # the isolated copy of the project
    home: Path  # fresh HOME for executed commands
    created: bool = True
    ignored: Tuple[str, ...] = DEFAULT_IGNORED_DIRS

    @classmethod
    def create(cls, project_root: str, workspace_dir: Optional[str] = None, ignored: Tuple[str, ...] = DEFAULT_IGNORED_DIRS) -> "Workspace":
        source = Path(project_root).resolve()
        if not source.is_dir():
            raise RemediationError(f"project root is not a directory: {source}")
        if workspace_dir:
            root = Path(workspace_dir).resolve()
            if root.exists() and any(root.iterdir()):
                raise RemediationError(f"workspace directory is not empty: {root}")
            if source == root or source in root.parents or root in source.parents:
                raise RemediationError("the workspace must not be inside the project nor contain it")
            root.mkdir(parents=True, exist_ok=True)
        else:
            root = Path(tempfile.mkdtemp(prefix="fa-remediate-"))
        project = root / "project"
        shutil.copytree(source, project, ignore=shutil.ignore_patterns(*ignored), symlinks=False)
        home = root / "home"
        home.mkdir()
        return cls(root=root, project=project, home=home, ignored=ignored)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def relative(self, path: Path) -> str:
        return path.relative_to(self.project).as_posix()


def _limits(policy: ExecutionPolicy):
    """preexec_fn applying CPU and file-size limits (POSIX only)."""
    try:
        import resource
    except ImportError:  # pragma: no cover - non-POSIX
        return None

    cpu = int(policy.cpu_seconds or policy.timeout_s * 2) or 1

    def apply() -> None:
        for kind, value in ((resource.RLIMIT_CPU, cpu), (resource.RLIMIT_FSIZE, policy.max_file_bytes)):
            try:
                soft, hard = resource.getrlimit(kind)
                limit = value if hard == resource.RLIM_INFINITY else min(value, hard)
                resource.setrlimit(kind, (limit, hard))
            except (ValueError, OSError):
                pass

    return apply


def sanitised_env(policy: ExecutionPolicy, workspace: Workspace) -> Dict[str, str]:
    env = {name: os.environ[name] for name in policy.env_passthrough if name in os.environ}
    env["HOME"] = str(workspace.home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    env["FASTAPI_AUDIT_REMEDIATION"] = "1"
    return env


def run_command(policy: ExecutionPolicy, workspace: Workspace) -> TestRun:
    """Execute the policy's command inside the copy; never called without ``policy.authorized``."""
    if not policy.authorized:
        return TestRun(ran=False, refused="execution not authorized")
    if not policy.command:
        return TestRun(ran=False, refused="no test command given")
    argv = parse_command(policy.command)
    refused = command_allowed(argv, policy.allowed_commands)
    if refused:
        return TestRun(ran=False, command=argv, refused=refused)
    try:
        proc = subprocess.run(
            argv,
            cwd=str(workspace.project),
            env=sanitised_env(policy, workspace),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=policy.timeout_s,
            preexec_fn=_limits(policy) if os.name == "posix" else None,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return TestRun(ran=True, command=argv, returncode=None, passed=False, timed_out=True, stdout_tail=out[-TAIL:], stderr_tail=err[-TAIL:])
    except (OSError, ValueError) as exc:
        return TestRun(ran=False, command=argv, refused=f"could not start the command: {exc}")
    return TestRun(ran=True, command=argv, returncode=proc.returncode, passed=proc.returncode == 0, stdout_tail=proc.stdout[-TAIL:], stderr_tail=proc.stderr[-TAIL:])


def digest_tree(root: str, suffixes: Tuple[str, ...] = (".py",), ignored: Tuple[str, ...] = DEFAULT_IGNORED_DIRS) -> Dict[str, str]:
    """``{relative path: sha256}`` of the project's source files; the pipeline proves the project was not touched."""
    base = Path(root).resolve()
    out: Dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in ignored)
        for name in sorted(filenames):
            if name.endswith(suffixes):
                path = Path(dirpath) / name
                try:
                    out[path.relative_to(base).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    continue
    return out
