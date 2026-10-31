"""Isolated execution in OCI containers (v2.1, increment 1): the ``container`` executor behind the same seam as the
local one, Docker as the first runtime, nothing coupled to its CLI outside ``DockerCliRuntime``.

What it does, in order, for every run:

1. validates the sandbox and image policies (a weakened mandatory restriction is refused before anything starts);
2. asks the runtime who it is (version, operating system, kernel, cgroup version, security options, rootless or
   not) and records it;
3. resolves the image (allow-listed reference or an explicit digest; pulled only when missing and allowed; the
   observed digests are recorded, and a configured digest must match);
4. proves the daemon can see the workspace (a short read-only probe container; with Colima or Docker Desktop the
   daemon runs in a VM that shares only some host paths);
5. prepares the project's dependencies **inside a container** (never on the host): a volume owned by the sandbox
   user receives a virtual environment built from the project's requirements files or ``pyproject.toml``
   (dependencies and the dev/test groups, never the project's build backend); network only in this phase and only
   as the policy allows; the pip cache volume is shared only for the ``fixture`` and ``trusted`` profiles (an
   untrusted project could poison it for later runs);
6. runs the authorized command in a second container with the network disabled, as a non-root user, on a
   read-only root filesystem, with every capability dropped, ``no-new-privileges``, the runtime's seccomp profile,
   memory, CPU, pids and file-size limits, a size-limited ``noexec`` tmpfs for ``/tmp``, and exactly three mounts:
   the workspace copy, the evidence directory and the prepared environment (read-only);
7. verifies the configuration the daemon reports for the created container against the policy **before starting
   it** (``verify_effective``), and verifies it again **from inside** with ``_sandbox_probe.py`` (uid, read-only
   root, bounding capabilities, no-new-privileges, seccomp mode, interfaces and a failed connection attempt,
   cgroup limits, no Docker socket, no host credentials, the environment keys) before the command is exec'ed;
8. reads the runner's nonce-bound report through the same adapters as the local executor, assesses the outcome
   from that report (never from the exit code alone), and removes the containers it created and the prepared
   volume when the workspace is released; everything is labelled so leftovers can be reaped.

What it does **not** provide, and says so in every run: protection against vulnerabilities of the container
runtime or of the kernel (the kernel is shared); a rootful daemon is root-equivalent on its host, so the sandbox
trusts the daemon and whoever controls its socket; egress during the preparation phase is not restricted to a
package index; a bind mount cannot be ``noexec`` with Docker, so the workspace copy is exec-capable; no user
namespace remapping (uid 65534 inside is uid 65534 on the daemon host); when the daemon runs in a VM, the host it
protects is that VM.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .executors import ISOLATION_ISOLATED, TRUST_FIXTURE, TRUST_TRUSTED, TRUST_UNTRUSTED, Executor
from .isolation import TAIL, ExecutionPolicy, Workspace, parse_command
from .model import OUTCOME_INFRASTRUCTURE_ERROR, OUTCOME_REFUSED, RemediationError, TestRun
from .runners import Evidence, PytestJUnitAdapter, adapter_for, assess

DEFAULT_IMAGE = "python:3.13-slim"
ALLOWED_IMAGES = ("python:3.13-slim", "python:3.12-slim", "python:3.11-slim")
IMAGE_INTERPRETER = "/usr/local/bin/python3"  # the official python images; the prepared environment is preferred through PATH
NETWORK_NONE = "none"
NETWORK_BRIDGE = "bridge"
LABEL_OWNER = "fastapi-architect.owner"
LABEL_NONCE = "fastapi-architect.nonce"
LABEL_PHASE = "fastapi-architect.phase"
LABEL_WORKSPACE = "fastapi-architect.workspace"
OWNER = "container-executor"
MOUNT_WORKSPACE = "/workspace"
MOUNT_EVIDENCE = "/evidence"
MOUNT_VENV = "/venv"
MOUNT_CACHE = "/cache"
MOUNT_PROBE = "/probe"
SANDBOX_PATH = f"{MOUNT_VENV}/env/bin:/usr/local/bin:/usr/bin:/bin"
TMPFS_OPTIONS = "rw,noexec,nosuid,nodev"
EXIT_PROBE_REJECTED = 97  # the in-container probe found a violated mandatory control and did not exec the command
EXIT_COMMAND_NOT_FOUND = 98
REQUIREMENTS_FILES = ("requirements.txt", "requirements-dev.txt", "requirements-test.txt", "dev-requirements.txt", "test-requirements.txt", "requirements/dev.txt", "requirements/test.txt")
PREPARE_GROUPS = "dev,test,tests"
SANDBOX_ALLOWED_PROGRAMS = ("python", "pytest")  # uv is not in the image; the local policy's extra programs are not honoured here
HOST_SECRET_DIRS = (".ssh", ".aws", ".docker", ".kube", ".gnupg", ".netrc", ".config/gcloud", ".azure")
FORBIDDEN_MOUNT_PREFIXES = ("/etc", "/root", "/proc", "/sys", "/dev", "/var/run", "/run", "/boot", "/usr", "/bin", "/sbin", "/lib")
PROBE_SCRIPT = Path(__file__).with_name("_sandbox_probe.py")
PREPARE_SCRIPT = Path(__file__).with_name("_prepare_env.py")
MIB = 1024 * 1024


class InfrastructureError(RemediationError):
    """The runtime, the image or the mounts are not usable; nothing ran, nothing is verified."""


class SecurityRejection(RemediationError):
    """A policy, an image or an effective configuration that would weaken a mandatory restriction; nothing ran."""


# --------------------------------------------------------------------------- #
# policies
# --------------------------------------------------------------------------- #


@dataclass
class ImagePolicy:
    reference: str = DEFAULT_IMAGE
    digest: Optional[str] = None  # sha256:...; when set the pulled image must carry it among its repository digests
    allowed_references: Tuple[str, ...] = ALLOWED_IMAGES
    pull: bool = True  # pull when the image is missing; False fails closed instead

    def validate(self) -> List[str]:
        errors: List[str] = []
        if not self.reference or any(c.isspace() for c in self.reference):
            errors.append(f"image reference {self.reference!r} is not valid")
        if self.digest is not None and not (self.digest.startswith("sha256:") and len(self.digest) == 71 and all(c in "0123456789abcdef" for c in self.digest[7:])):
            errors.append(f"image digest {self.digest!r} is not a sha256 digest")
        if self.reference not in self.allowed_references and not self.digest:
            errors.append(f"image {self.reference!r} is not in the allow-list ({', '.join(self.allowed_references)}) and no digest pins it")
        return errors

    @property
    def pinned_reference(self) -> str:
        if not self.digest:
            return self.reference
        repository = self.reference.rsplit("@", 1)[0]
        if ":" in repository.rsplit("/", 1)[-1]:
            repository = repository.rsplit(":", 1)[0]
        return f"{repository}@{self.digest}"

    def to_dict(self) -> Dict[str, object]:
        return {"reference": self.reference, "digest": self.digest, "allowed_references": list(self.allowed_references), "pull": self.pull}


@dataclass
class SandboxPolicy:
    """Every field is a restriction the executor applies; the mandatory ones cannot be weakened (``validate``)."""

    user: str = "65534:65534"  # uid:gid; "host" means the invoking user's ids (refused when that is root)
    read_only_root: bool = True
    drop_all_capabilities: bool = True
    no_new_privileges: bool = True
    seccomp: str = "builtin"  # the runtime's built-in profile; a profile path is accepted; "unconfined" is refused
    network_run: str = NETWORK_NONE
    network_prepare: str = NETWORK_BRIDGE  # none when the dependencies are already in the image or none are needed
    memory_mb: int = 512
    prepare_memory_mb: int = 1024
    cpus: float = 1.0
    pids_limit: int = 256
    tmp_size_mb: int = 256
    prepare_timeout_s: float = 900.0
    workspace_base: Optional[str] = None  # default: ~/.cache/fastapi-architect/workspaces (a path the daemon can see)
    pip_cache_volume: Optional[str] = "fastapi-architect-pip-cache"  # shared only for fixture/trusted runs; None disables
    interpreter: str = IMAGE_INTERPRETER
    privileged: bool = False  # exists so that a weakened configuration can be named and refused
    extra_mounts: Tuple[str, ...] = ()  # operator-provided mounts are not supported in this version: always refused

    def resolved_user(self) -> str:
        if self.user == "host":
            if os.name != "posix":
                return "65534:65534"
            return f"{os.getuid()}:{os.getgid()}"
        return self.user

    def uid_gid(self) -> Tuple[int, int]:
        parts = self.resolved_user().split(":")
        try:
            uid = int(parts[0])
            gid = int(parts[1]) if len(parts) > 1 else uid
        except ValueError:
            return (-1, -1)
        return (uid, gid)

    def validate(self) -> List[str]:
        errors: List[str] = []
        uid, gid = self.uid_gid()
        if uid < 0 or gid < 0:
            errors.append(f"sandbox user {self.user!r} must be numeric uid:gid")
        elif uid == 0 or gid == 0:
            errors.append("sandbox user must not be root (uid or gid 0)")
        if not self.read_only_root:
            errors.append("read_only_root is mandatory")
        if not self.drop_all_capabilities:
            errors.append("drop_all_capabilities is mandatory")
        if not self.no_new_privileges:
            errors.append("no_new_privileges is mandatory")
        if self.seccomp in ("unconfined", "", None):
            errors.append("seccomp must be the runtime's built-in profile or a profile path, never unconfined")
        if self.network_run != NETWORK_NONE:
            errors.append(f"network_run must be {NETWORK_NONE!r} (got {self.network_run!r})")
        if self.network_prepare not in (NETWORK_NONE, NETWORK_BRIDGE):
            errors.append(f"network_prepare must be {NETWORK_NONE!r} or {NETWORK_BRIDGE!r} (got {self.network_prepare!r})")
        if self.memory_mb <= 0 or self.prepare_memory_mb <= 0:
            errors.append("memory limits must be positive")
        if self.cpus <= 0:
            errors.append("cpus must be positive")
        if self.pids_limit <= 0:
            errors.append("pids_limit must be positive")
        if self.tmp_size_mb <= 0:
            errors.append("tmp_size_mb must be positive")
        if self.privileged:
            errors.append("privileged containers are refused")
        if self.extra_mounts:
            errors.append("operator-provided mounts are refused in this version")
        return errors

    def base_dir(self) -> Path:
        if self.workspace_base:
            return Path(self.workspace_base).expanduser().resolve()
        return (Path.home() / ".cache" / "fastapi-architect" / "workspaces").resolve()

    def to_dict(self) -> Dict[str, object]:
        return {
            "user": self.resolved_user(),
            "read_only_root": self.read_only_root,
            "drop_all_capabilities": self.drop_all_capabilities,
            "no_new_privileges": self.no_new_privileges,
            "seccomp": self.seccomp,
            "network_run": self.network_run,
            "network_prepare": self.network_prepare,
            "memory_mb": self.memory_mb,
            "prepare_memory_mb": self.prepare_memory_mb,
            "cpus": self.cpus,
            "pids_limit": self.pids_limit,
            "tmp_size_mb": self.tmp_size_mb,
            "prepare_timeout_s": self.prepare_timeout_s,
            "workspace_base": str(self.base_dir()),
            "pip_cache_volume": self.pip_cache_volume,
            "interpreter": self.interpreter,
        }


# --------------------------------------------------------------------------- #
# runtime-neutral specification and results
# --------------------------------------------------------------------------- #


@dataclass
class Mount:
    source: str  # host path (bind) or volume name (volume)
    target: str
    read_only: bool = False
    kind: str = "bind"  # bind | volume

    def to_dict(self) -> Dict[str, object]:
        return {"kind": self.kind, "source": self.source, "target": self.target, "read_only": self.read_only}


@dataclass
class ContainerSpec:
    name: str
    image: str
    argv: List[str]
    user: str
    network: str
    workdir: str = MOUNT_WORKSPACE
    env: Dict[str, str] = field(default_factory=dict)
    read_only_root: bool = True
    drop_all_capabilities: bool = True
    cap_add: Tuple[str, ...] = ()
    no_new_privileges: bool = True
    seccomp: str = "builtin"
    memory_mb: int = 512
    cpus: float = 1.0
    pids_limit: int = 256
    mounts: List[Mount] = field(default_factory=list)
    tmpfs: Dict[str, str] = field(default_factory=dict)  # path -> options
    labels: Dict[str, str] = field(default_factory=dict)
    ulimits: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "image": self.image,
            "argv": list(self.argv),
            "user": self.user,
            "network": self.network,
            "workdir": self.workdir,
            "env_keys": sorted(self.env),
            "read_only_root": self.read_only_root,
            "drop_all_capabilities": self.drop_all_capabilities,
            "cap_add": list(self.cap_add),
            "no_new_privileges": self.no_new_privileges,
            "seccomp": self.seccomp,
            "memory_mb": self.memory_mb,
            "cpus": self.cpus,
            "pids_limit": self.pids_limit,
            "mounts": [m.to_dict() for m in self.mounts],
            "tmpfs": dict(self.tmpfs),
            "labels": dict(self.labels),
            "ulimits": dict(self.ulimits),
        }


@dataclass
class ContainerState:
    container_id: str
    exit_code: Optional[int] = None
    oom_killed: bool = False
    status: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {"container_id": self.container_id, "exit_code": self.exit_code, "oom_killed": self.oom_killed, "status": self.status, "error": self.error}


@dataclass
class ImageIdentity:
    reference: str
    image_id: str
    repo_digests: List[str]
    pinned: bool
    created: str = ""
    platform: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {"reference": self.reference, "id": self.image_id, "repo_digests": list(self.repo_digests), "pinned_by_digest": self.pinned, "created": self.created, "platform": self.platform, "provenance": "repository digest recorded; signatures are not verified" + ("" if self.pinned else "; not pinned by the operator")}


@dataclass
class RuntimeInfo:
    name: str
    available: bool = False
    client_version: str = ""
    server_version: str = ""
    operating_system: str = ""
    kernel: str = ""
    architecture: str = ""
    cgroup_version: str = ""
    security_options: List[str] = field(default_factory=list)
    rootless: bool = False
    endpoint: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "available": self.available,
            "client_version": self.client_version,
            "server_version": self.server_version,
            "operating_system": self.operating_system,
            "kernel": self.kernel,
            "architecture": self.architecture,
            "cgroup_version": self.cgroup_version,
            "security_options": list(self.security_options),
            "rootless": self.rootless,
            "endpoint": self.endpoint,
            "trust_boundary": ("rootless daemon: a breakout reaches the daemon user, not root" if self.rootless else "rootful daemon: it is root-equivalent on its host; the sandbox trusts the daemon and whoever controls its socket"),
            "error": self.error,
        }


class ContainerRuntime:
    """Interface of an OCI runtime adapter. ``DockerCliRuntime`` is the only implementation in this version; a
    Podman adapter fits behind the same methods."""

    name = "abstract"

    def info(self) -> RuntimeInfo:  # pragma: no cover - interface
        raise NotImplementedError

    def ensure_image(self, policy: ImagePolicy) -> ImageIdentity:  # pragma: no cover - interface
        raise NotImplementedError

    def create(self, spec: ContainerSpec) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def inspect(self, container_id: str) -> Dict[str, object]:  # pragma: no cover - interface
        raise NotImplementedError

    def start_and_wait(self, container_id: str, timeout_s: float) -> Tuple[str, str, bool]:  # pragma: no cover - interface
        """``(stdout, stderr, timed_out)``; a timed-out container has been killed when this returns."""
        raise NotImplementedError

    def state(self, container_id: str) -> ContainerState:  # pragma: no cover - interface
        raise NotImplementedError

    def remove(self, container_id: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def volume_create(self, name: str, labels: Dict[str, str]) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def volume_exists(self, name: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def volume_remove(self, name: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def list_labelled(self, kind: str, label: str, value: str) -> List[str]:  # pragma: no cover - interface
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Docker through its CLI
# --------------------------------------------------------------------------- #


CLIENT_ENV = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY", "TMPDIR", "LANG", "LC_ALL")


class DockerCliRuntime(ContainerRuntime):
    name = "docker"

    def __init__(self, binary: Optional[str] = None, env: Optional[Dict[str, str]] = None, command_timeout_s: float = 120.0, pull_timeout_s: float = 900.0) -> None:
        self.binary = binary or shutil.which("docker") or "docker"
        self.env_override = dict(env or {})
        self.command_timeout_s = command_timeout_s
        self.pull_timeout_s = pull_timeout_s

    # -- plumbing ---------------------------------------------------------- #

    def client_env(self) -> Dict[str, str]:
        env = {name: os.environ[name] for name in CLIENT_ENV if name in os.environ}
        env.update(self.env_override)
        return env

    def _run(self, *args: str, timeout: Optional[float] = None, check: bool = True) -> "subprocess.CompletedProcess[str]":
        argv = [self.binary, *args]
        try:
            proc = subprocess.run(argv, env=self.client_env(), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout or self.command_timeout_s)
        except FileNotFoundError as exc:
            raise InfrastructureError(f"container runtime binary not found: {self.binary}") from exc
        except subprocess.TimeoutExpired as exc:
            raise InfrastructureError(f"runtime command timed out after {timeout or self.command_timeout_s:.0f}s: {' '.join(args[:3])}") from exc
        except OSError as exc:
            raise InfrastructureError(f"cannot run the container runtime: {exc}") from exc
        if check and proc.returncode != 0:
            raise InfrastructureError(f"docker {' '.join(args[:2])} failed ({proc.returncode}): {(proc.stderr or proc.stdout).strip()[-600:]}")
        return proc

    # -- identity ---------------------------------------------------------- #

    def info(self) -> RuntimeInfo:
        info = RuntimeInfo(name=self.name)
        try:
            version = json.loads(self._run("version", "--format", "json").stdout or "{}")
            daemon = json.loads(self._run("info", "--format", "json").stdout or "{}")
        except (InfrastructureError, ValueError) as exc:
            info.error = str(exc)
            raise InfrastructureError(f"docker daemon not usable: {exc}") from exc
        server = version.get("Server") or {}
        client = version.get("Client") or {}
        if not server:
            raise InfrastructureError("docker client answered but no daemon did (is the daemon running and the context right?)")
        info.available = True
        info.client_version = str(client.get("Version", ""))
        info.server_version = str(server.get("Version", daemon.get("ServerVersion", "")))
        info.operating_system = str(daemon.get("OperatingSystem", ""))
        info.kernel = str(daemon.get("KernelVersion", ""))
        info.architecture = str(daemon.get("Architecture", ""))
        info.cgroup_version = str(daemon.get("CgroupVersion", ""))
        info.security_options = [str(o) for o in daemon.get("SecurityOptions") or []]
        info.rootless = any("rootless" in o for o in info.security_options)
        info.endpoint = str(daemon.get("Name", "")) + (f" (context {self.env_override.get('DOCKER_CONTEXT') or os.environ.get('DOCKER_CONTEXT') or 'default'})")
        return info

    def ensure_image(self, policy: ImagePolicy) -> ImageIdentity:
        errors = policy.validate()
        if errors:
            raise SecurityRejection("; ".join(errors))
        reference = policy.pinned_reference
        probe = self._run("image", "inspect", reference, "--format", "json", check=False)
        if probe.returncode != 0:
            if not policy.pull:
                raise InfrastructureError(f"image {reference} is not present and pulling is disabled by the policy")
            pulled = self._run("pull", "--quiet", reference, timeout=self.pull_timeout_s, check=False)
            if pulled.returncode != 0:
                detail = (pulled.stderr or pulled.stdout).strip()[-400:]
                if policy.digest and any(marker in detail.lower() for marker in ("manifest unknown", "not found", "no such manifest", "manifest for")):
                    raise SecurityRejection(f"the pinned digest {policy.digest} does not exist for {policy.reference.rsplit(':', 1)[0]}: the image identity the policy requires is not available ({detail})")
                raise InfrastructureError(f"image {reference} could not be pulled: {detail}")
            probe = self._run("image", "inspect", reference, "--format", "json")
        try:
            documents = json.loads(probe.stdout or "[]")
        except ValueError as exc:
            raise InfrastructureError(f"image inspect returned no JSON: {exc}") from exc
        document = documents[0] if isinstance(documents, list) and documents else (documents if isinstance(documents, dict) else {})
        repo_digests = [str(d) for d in document.get("RepoDigests") or []]
        if policy.digest and not any(d.endswith("@" + policy.digest) for d in repo_digests):
            raise SecurityRejection(f"image {reference} does not carry the configured digest {policy.digest} (repository digests: {repo_digests or 'none'})")
        return ImageIdentity(
            reference=reference,
            image_id=str(document.get("Id", "")),
            repo_digests=repo_digests,
            pinned=bool(policy.digest),
            created=str(document.get("Created", "")),
            platform=f"{document.get('Os', '')}/{document.get('Architecture', '')}",
        )

    # -- containers -------------------------------------------------------- #

    def create_argv(self, spec: ContainerSpec) -> List[str]:
        """The exact ``docker create`` invocation for a spec (pure; tested without a daemon)."""
        argv = [self.binary, "create", "--name", spec.name, "--user", spec.user, "--network", spec.network, "--workdir", spec.workdir, "--memory", f"{spec.memory_mb}m", "--memory-swap", f"{spec.memory_mb}m", "--cpus", str(spec.cpus), "--pids-limit", str(spec.pids_limit), "--ipc", "private", "--stop-timeout", "5"]
        if spec.read_only_root:
            argv.append("--read-only")
        if spec.drop_all_capabilities:
            argv += ["--cap-drop", "ALL"]
        for cap in spec.cap_add:
            argv += ["--cap-add", cap]
        if spec.no_new_privileges:
            argv += ["--security-opt", "no-new-privileges"]
        if spec.seccomp:
            argv += ["--security-opt", f"seccomp={spec.seccomp}"]
        for key in sorted(spec.env):
            argv += ["--env", f"{key}={spec.env[key]}"]
        for mount in spec.mounts:
            parts = [f"type={mount.kind}", f"source={mount.source}", f"target={mount.target}"]
            if mount.read_only:
                parts.append("readonly")
            argv += ["--mount", ",".join(parts)]
        for path in sorted(spec.tmpfs):
            argv += ["--tmpfs", f"{path}:{spec.tmpfs[path]}"]
        for key in sorted(spec.labels):
            argv += ["--label", f"{key}={spec.labels[key]}"]
        for key in sorted(spec.ulimits):
            argv += ["--ulimit", f"{key}={spec.ulimits[key]}"]
        argv.append(spec.image)
        argv.extend(spec.argv)
        return argv

    def create(self, spec: ContainerSpec) -> str:
        proc = self._run(*self.create_argv(spec)[1:])
        container_id = (proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else ""
        if not container_id:
            raise InfrastructureError("docker create returned no container id")
        return container_id

    def inspect(self, container_id: str) -> Dict[str, object]:
        proc = self._run("inspect", "--format", "json", container_id)
        try:
            documents = json.loads(proc.stdout or "[]")
        except ValueError as exc:
            raise InfrastructureError(f"docker inspect returned no JSON: {exc}") from exc
        if isinstance(documents, list):
            return documents[0] if documents else {}
        return documents if isinstance(documents, dict) else {}

    def start_and_wait(self, container_id: str, timeout_s: float) -> Tuple[str, str, bool]:
        argv = [self.binary, "start", "--attach", container_id]
        try:
            proc = subprocess.run(argv, env=self.client_env(), stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            self._run("kill", container_id, check=False, timeout=30)
            out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return out, err, True
        except OSError as exc:
            raise InfrastructureError(f"cannot start the container: {exc}") from exc
        return proc.stdout or "", proc.stderr or "", False

    def state(self, container_id: str) -> ContainerState:
        document = self.inspect(container_id)
        state = document.get("State") if isinstance(document, dict) else {}
        state = state if isinstance(state, dict) else {}
        return ContainerState(container_id=container_id, exit_code=state.get("ExitCode"), oom_killed=bool(state.get("OOMKilled")), status=str(state.get("Status", "")), error=str(state.get("Error", "")))

    def remove(self, container_id: str) -> bool:
        proc = self._run("rm", "--force", "--volumes", container_id, check=False, timeout=60)
        return proc.returncode == 0

    # -- volumes ----------------------------------------------------------- #

    def volume_create(self, name: str, labels: Dict[str, str]) -> None:
        args = ["volume", "create"]
        for key in sorted(labels):
            args += ["--label", f"{key}={labels[key]}"]
        self._run(*args, name)

    def volume_exists(self, name: str) -> bool:
        return self._run("volume", "inspect", name, check=False).returncode == 0

    def volume_remove(self, name: str) -> bool:
        return self._run("volume", "rm", "--force", name, check=False, timeout=60).returncode == 0

    def list_labelled(self, kind: str, label: str, value: str) -> List[str]:
        if kind == "container":
            proc = self._run("ps", "--all", "--quiet", "--no-trunc", "--filter", f"label={label}={value}", check=False)
        else:
            proc = self._run("volume", "ls", "--quiet", "--filter", f"label={label}={value}", check=False)
        return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# verification of the effective configuration
# --------------------------------------------------------------------------- #


def forbidden_mount_sources() -> Tuple[List[str], List[str]]:
    """``(exact, prefixes)`` of host paths that must never be a mount source: the root and the home directory
    themselves (exact), the system directories, the Docker socket and the home's credential directories (as
    prefixes). A workspace under ``~/.cache`` is legitimate; ``~/.ssh`` or anything under ``/run`` is not."""
    exact = ["/"]
    prefixes = [_resolved(p) for p in FORBIDDEN_MOUNT_PREFIXES] + list(FORBIDDEN_MOUNT_PREFIXES)
    try:
        home = Path.home().resolve()
        exact.append(str(home))
        prefixes.extend(str(home / name) for name in HOST_SECRET_DIRS)
    except (OSError, RuntimeError):
        pass
    return exact, prefixes


def source_is_forbidden(source: str) -> bool:
    exact, prefixes = forbidden_mount_sources()
    resolved = _resolved(source)
    if resolved in exact or source in exact:
        return True
    return any(resolved == p or resolved.startswith(p.rstrip("/") + "/") for p in prefixes)


def _resolved(path: str) -> str:
    try:
        return str(Path(path).resolve())
    except OSError:
        return path


def verify_effective(document: Dict[str, object], spec: ContainerSpec) -> List[str]:
    """Compare what the daemon reports for a created container with the specification. Returns the violations;
    an empty list means every mandatory restriction is in force. Pure: tested on recorded inspect documents."""
    violations: List[str] = []
    config = document.get("Config") if isinstance(document.get("Config"), dict) else {}
    host = document.get("HostConfig") if isinstance(document.get("HostConfig"), dict) else {}
    if str(config.get("User", "")) != spec.user:
        violations.append(f"user is {config.get('User')!r}, expected {spec.user!r}")
    if spec.read_only_root and host.get("ReadonlyRootfs") is not True:
        violations.append("root filesystem is not read-only")
    if host.get("Privileged"):
        violations.append("container is privileged")
    drops = [str(c).upper() for c in host.get("CapDrop") or []]
    if spec.drop_all_capabilities and "ALL" not in drops:
        violations.append(f"capabilities are not all dropped (CapDrop={drops})")
    adds = sorted(str(c).upper().replace("CAP_", "") for c in host.get("CapAdd") or [])
    if adds != sorted(c.upper().replace("CAP_", "") for c in spec.cap_add):
        violations.append(f"capabilities added {adds} differ from the specification {list(spec.cap_add)}")
    secopts = [str(o) for o in host.get("SecurityOpt") or []]
    if spec.no_new_privileges and not any(o.startswith("no-new-privileges") for o in secopts):
        violations.append("no-new-privileges is not set")
    if any("unconfined" in o for o in secopts):
        violations.append(f"a security option is unconfined: {secopts}")
    if str(host.get("NetworkMode", "")) != spec.network:
        violations.append(f"network mode is {host.get('NetworkMode')!r}, expected {spec.network!r}")
    expected_memory = spec.memory_mb * MIB
    if host.get("Memory") != expected_memory:
        violations.append(f"memory limit is {host.get('Memory')}, expected {expected_memory}")
    if host.get("MemorySwap") != expected_memory:
        violations.append(f"memory+swap limit is {host.get('MemorySwap')}, expected {expected_memory} (no swap)")
    if host.get("PidsLimit") != spec.pids_limit:
        violations.append(f"pids limit is {host.get('PidsLimit')}, expected {spec.pids_limit}")
    if host.get("NanoCpus") != int(round(spec.cpus * 1e9)):
        violations.append(f"cpu quota is {host.get('NanoCpus')} nanocpus, expected {int(round(spec.cpus * 1e9))}")
    if host.get("Devices"):
        violations.append(f"devices are exposed: {host.get('Devices')}")
    for mode_key in ("PidMode", "UTSMode", "UsernsMode", "CgroupnsMode", "IpcMode"):
        if str(host.get(mode_key, "")) == "host":
            violations.append(f"{mode_key} shares the host namespace")
    tmpfs = host.get("Tmpfs") if isinstance(host.get("Tmpfs"), dict) else {}
    for path, options in spec.tmpfs.items():
        actual = str(tmpfs.get(path, ""))
        missing = [flag for flag in ("noexec", "nosuid", "nodev") if flag in options and flag not in actual]
        if path not in tmpfs or missing:
            violations.append(f"tmpfs {path} lacks {missing or 'the mount'} (got {actual!r})")
    mounts = document.get("Mounts") if isinstance(document.get("Mounts"), list) else []
    expected_binds = {_resolved(m.source): m for m in spec.mounts if m.kind == "bind"}
    expected_volumes = {m.source: m for m in spec.mounts if m.kind == "volume"}
    seen = 0
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        seen += 1
        kind = str(mount.get("Type", ""))
        source = str(mount.get("Source", ""))
        destination = str(mount.get("Destination", ""))
        writable = bool(mount.get("RW", True))
        if kind == "bind":
            if source_is_forbidden(source):
                violations.append(f"bind mount from a forbidden host path: {source}")
            expected = expected_binds.get(_resolved(source))
            if expected is None:
                violations.append(f"unexpected bind mount {source} -> {destination}")
            elif expected.target != destination or expected.read_only == writable:
                violations.append(f"bind mount {source} -> {destination} ({'rw' if writable else 'ro'}) differs from the specification")
        elif kind == "volume":
            expected = expected_volumes.get(str(mount.get("Name", "")))
            if expected is None:
                violations.append(f"unexpected volume mount {mount.get('Name')} -> {destination}")
            elif expected.target != destination or expected.read_only == writable:
                violations.append(f"volume mount {mount.get('Name')} -> {destination} ({'rw' if writable else 'ro'}) differs from the specification")
        elif kind == "tmpfs":
            seen -= 1  # tmpfs mounts are checked through HostConfig.Tmpfs
        else:
            violations.append(f"unexpected mount of type {kind!r}: {source} -> {destination}")
    if seen != len(spec.mounts):
        violations.append(f"{seen} mounts reported, {len(spec.mounts)} specified")
    return violations


def summarize_effective(document: Dict[str, object]) -> Dict[str, object]:
    host = document.get("HostConfig") if isinstance(document.get("HostConfig"), dict) else {}
    config = document.get("Config") if isinstance(document.get("Config"), dict) else {}
    return {
        "user": config.get("User"),
        "read_only_root": host.get("ReadonlyRootfs"),
        "privileged": host.get("Privileged"),
        "cap_drop": host.get("CapDrop"),
        "cap_add": host.get("CapAdd"),
        "security_opt": host.get("SecurityOpt"),
        "network_mode": host.get("NetworkMode"),
        "memory_bytes": host.get("Memory"),
        "memory_swap_bytes": host.get("MemorySwap"),
        "nano_cpus": host.get("NanoCpus"),
        "pids_limit": host.get("PidsLimit"),
        "tmpfs": host.get("Tmpfs"),
        "ulimits": host.get("Ulimits"),
        "mounts": [{"type": m.get("Type"), "source": m.get("Source") or m.get("Name"), "destination": m.get("Destination"), "rw": m.get("RW")} for m in (document.get("Mounts") or []) if isinstance(m, dict)],
        "userns_mode": host.get("UsernsMode"),
        "cgroupns_mode": host.get("CgroupnsMode"),
    }


# --------------------------------------------------------------------------- #
# the executor
# --------------------------------------------------------------------------- #


def _program_allowed(argv: Sequence[str]) -> Optional[str]:
    if not argv:
        return "empty command"
    program = os.path.basename(argv[0])
    if program.startswith("python") or program in SANDBOX_ALLOWED_PROGRAMS:
        return None
    return f"program {argv[0]!r} is not available in the sandbox image (allowed: python, pytest)"


def _chmod_world(path: Path, mode: int) -> None:
    try:
        os.chmod(str(path), mode)
    except OSError:
        pass


class ContainerExecutor(Executor):
    name = "container"
    isolation_level = ISOLATION_ISOLATED
    guarantees = (
        "runs in an OCI container as a non-root user on a read-only root filesystem, every capability dropped, no-new-privileges, the runtime's seccomp profile",
        "network disabled for the test run; dependencies prepared in a separate container, never on the host",
        "memory, CPU, pids, file-size and wall-clock limits; /tmp is a size-limited noexec tmpfs",
        "exactly three mounts: the workspace copy, the evidence directory and the prepared environment (read-only); no Docker socket, no host HOME or credentials",
        "the daemon-reported configuration is verified before the container starts and the restrictions are verified again from inside before the command runs",
        "the runner's nonce-bound report decides the outcome; containers and prepared volumes are labelled and removed",
    )
    not_provided = (
        "protection against vulnerabilities of the container runtime or the kernel (the kernel is shared)",
        "protection from the daemon: a rootful daemon is root-equivalent on its host and whoever controls its socket controls the sandbox",
        "egress filtering during the preparation phase (it needs a package index; it is not restricted to one)",
        "image provenance beyond the repository digest (signatures are not verified; pin a digest to make the image identity a precondition)",
        "user namespace remapping (uid 65534 inside is uid 65534 on the daemon host) and a noexec workspace mount (Docker bind mounts cannot be noexec)",
    )

    def __init__(self, runtime: Optional[ContainerRuntime] = None, sandbox: Optional[SandboxPolicy] = None, image: Optional[ImagePolicy] = None) -> None:
        self.runtime = runtime or DockerCliRuntime()
        self.sandbox = sandbox or SandboxPolicy()
        self.image = image or ImagePolicy()
        self.granted_trust = TRUST_UNTRUSTED
        self._info: Optional[RuntimeInfo] = None
        self._identity: Optional[ImageIdentity] = None
        self._visible_bases: Dict[str, bool] = {}
        self._prepared: Dict[str, Dict[str, object]] = {}  # workspace root -> {"volume": name, "report": prepare record}

    # -- Executor contract -------------------------------------------------- #

    def accepts(self, trust: str) -> Optional[str]:
        if trust not in (TRUST_FIXTURE, TRUST_TRUSTED, TRUST_UNTRUSTED):
            return f"unknown trust profile {trust!r}"
        self.granted_trust = trust
        return None

    def workspace_dir(self) -> Optional[str]:
        base = self.sandbox.base_dir()
        base.mkdir(parents=True, exist_ok=True)
        _chmod_world(base, 0o755)
        return str(base / f"ws-{uuid.uuid4().hex[:12]}")

    def release(self, workspace: Workspace) -> None:
        prepared = self._prepared.pop(str(workspace.root), None)
        if prepared and prepared.get("volume"):
            try:
                self.runtime.volume_remove(str(prepared["volume"]))
            except InfrastructureError:
                pass

    def to_dict(self) -> Dict[str, object]:
        data = super().to_dict()
        data.update({"runtime": self.runtime.name, "sandbox": self.sandbox.to_dict(), "image": self.image.to_dict()})
        return data

    # -- run ----------------------------------------------------------------- #

    def run(self, command: str, workspace: Workspace, policy: ExecutionPolicy) -> TestRun:
        nonce = uuid.uuid4().hex
        argv = parse_command(command)
        environment: Dict[str, object] = {
            "executor": self.name,
            "isolation_level": self.isolation_level,
            "nonce": nonce,
            "trust_profile": self.granted_trust,
            "runtime": None,
            "image": None,
            "sandbox_policy": self.sandbox.to_dict(),
            "phases": {},
            "effective": None,
            "guarantees_applied": [],
            "limitations": list(self.not_provided),
            "cleanup": {"containers_removed": [], "volumes_removed": [], "volumes_kept": [], "leftovers": []},
            "rejection": None,
        }
        created: List[str] = []
        run = TestRun(ran=False, command=list(argv), executor=self.name)
        try:
            errors = self.sandbox.validate() + self.image.validate()
            if errors:
                raise SecurityRejection("policy refused: " + "; ".join(errors))
            if not policy.authorized:
                run.refused = "execution not authorized"
                run.outcome = assess(run, None)
                return run
            refused = _program_allowed(argv)
            if refused:
                run.refused = refused
                run.outcome = assess(run, None)
                return run
            info = self._runtime_info()
            environment["runtime"] = info.to_dict()
            identity = self._image_identity()
            environment["image"] = identity.to_dict()
            self._assert_workspace_visible(workspace)
            evidence_dir = self._evidence_dir(workspace)
            adapter = adapter_for(argv)
            if adapter is not None:
                instrumented, report_path = adapter.instrument(argv, evidence_dir, nonce, mount_point=Path(MOUNT_EVIDENCE))
            else:
                instrumented, report_path = list(argv), None
            needs_runner = isinstance(adapter, PytestJUnitAdapter)
            prepared = self._prepare(workspace, nonce, identity, environment, needs_runner, created)
            spec = self._run_spec(workspace, nonce, identity, instrumented, prepared)
            environment["phases"]["run"] = {"spec": spec.to_dict()}
            container_id = self.runtime.create(spec)
            created.append(container_id)
            environment["phases"]["run"]["container_id"] = container_id
            document = self.runtime.inspect(container_id)
            violations = verify_effective(document, spec)
            environment["phases"]["run"]["daemon_view"] = summarize_effective(document)
            if violations:
                raise SecurityRejection("the created container does not match the policy: " + "; ".join(violations))
            started = time.time()
            stdout, stderr, timed_out = self.runtime.start_and_wait(container_id, policy.timeout_s)
            finished = time.time()
            state = self.runtime.state(container_id)
            probe = self._read_json(evidence_dir / f"probe-{nonce}.json")
            environment["effective"] = probe
            environment["phases"]["run"].update({"started_at": started, "duration_s": round(finished - started, 3), "exit_code": state.exit_code, "oom_killed": state.oom_killed, "status": state.status, "termination": "timeout" if timed_out else ("oom_killed" if state.oom_killed else ("probe_rejected" if state.exit_code == EXIT_PROBE_REJECTED else ("command_not_found" if state.exit_code == EXIT_COMMAND_NOT_FOUND else "exited")))})
            if state.exit_code == EXIT_PROBE_REJECTED:
                raise SecurityRejection("the in-container probe found violated controls: " + "; ".join(str(v) for v in (probe or {}).get("violations", [])))
            if state.exit_code == EXIT_COMMAND_NOT_FOUND and not timed_out:
                raise InfrastructureError(f"the command is not available in the sandbox: {' '.join(argv[:2])} ({(stderr or stdout).strip()[-300:]})")
            run = TestRun(ran=True, command=list(argv), instrumented_command=list(instrumented), returncode=state.exit_code, passed=state.exit_code == 0, timed_out=timed_out, stdout_tail=stdout[-TAIL:], stderr_tail=stderr[-TAIL:], executor=self.name)
            evidence = adapter.collect(report_path, nonce, started) if adapter is not None and report_path is not None else Evidence(adapter=None)
            run.evidence = evidence.to_dict()
            run.counts = {k: v for k, v in evidence.counts.items() if v is not None}
            run.runner = adapter.name if adapter else "generic"
            run.outcome = assess(run, evidence)
            if state.oom_killed and run.outcome != "passed":
                run.outcome = OUTCOME_INFRASTRUCTURE_ERROR if not evidence.verifiable else run.outcome
            run.passed = run.outcome == "passed"
            environment["guarantees_applied"] = self._guarantees(probe, document, spec, prepared)
        except SecurityRejection as exc:
            run = TestRun(ran=False, command=list(argv), refused=f"security rejection: {exc}", executor=self.name)
            run.outcome = OUTCOME_REFUSED
            environment["rejection"] = {"kind": "security", "reason": str(exc)}
        except InfrastructureError as exc:
            run = TestRun(ran=False, command=list(argv), executor=self.name)
            run.outcome = OUTCOME_INFRASTRUCTURE_ERROR
            run.stderr_tail = str(exc)[-TAIL:]
            environment["rejection"] = {"kind": "infrastructure", "reason": str(exc)}
        finally:
            self._cleanup(created, environment)
        run.executor = self.name
        run.environment = environment
        return run

    # -- phases -------------------------------------------------------------- #

    def _runtime_info(self) -> RuntimeInfo:
        if self._info is None:
            self._info = self.runtime.info()
        return self._info

    def _image_identity(self) -> ImageIdentity:
        if self._identity is None:
            self._identity = self.runtime.ensure_image(self.image)
        return self._identity

    def _evidence_dir(self, workspace: Workspace) -> Path:
        evidence = workspace.root / "evidence"
        evidence.mkdir(exist_ok=True)
        _chmod_world(evidence, 0o777)
        for script in (PROBE_SCRIPT, PREPARE_SCRIPT):
            target = evidence / script.name
            if not target.exists():
                shutil.copyfile(script, target)
                _chmod_world(target, 0o644)
        return evidence

    def _common_env(self, home: str = "/tmp") -> Dict[str, str]:
        env = {"PATH": SANDBOX_PATH, "HOME": home, "TMPDIR": "/tmp", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0", "PYTHONNOUSERSITE": "1", "FASTAPI_AUDIT_REMEDIATION": "1", "FASTAPI_ARCHITECT_SANDBOX": "container"}
        for name in ("LANG", "LC_ALL", "LC_CTYPE"):
            if name in os.environ:
                env[name] = os.environ[name]
        return env

    def _labels(self, nonce: str, phase: str, workspace: Workspace) -> Dict[str, str]:
        return {LABEL_OWNER: OWNER, LABEL_NONCE: nonce, LABEL_PHASE: phase, LABEL_WORKSPACE: workspace.root.name}

    def _base_spec(self, name: str, nonce: str, phase: str, workspace: Workspace, image: ImageIdentity, argv: List[str], network: str, memory_mb: int, tmp_mb: int, policy_timeout_s: float) -> ContainerSpec:
        return ContainerSpec(
            name=name,
            image=image.reference,
            argv=argv,
            user=self.sandbox.resolved_user(),
            network=network,
            env=self._common_env(),
            read_only_root=self.sandbox.read_only_root,
            drop_all_capabilities=self.sandbox.drop_all_capabilities,
            no_new_privileges=self.sandbox.no_new_privileges,
            seccomp=self.sandbox.seccomp,
            memory_mb=memory_mb,
            cpus=self.sandbox.cpus,
            pids_limit=self.sandbox.pids_limit,
            tmpfs={"/tmp": f"{TMPFS_OPTIONS},size={tmp_mb}m"},
            labels=self._labels(nonce, phase, workspace),
            ulimits={"nofile": "4096:4096", "fsize": str(512 * MIB), "cpu": str(int(max(1, policy_timeout_s * 2)))},
        )

    def _probe_argv(self, nonce: str, suffix: str, network: str, inner: List[str]) -> List[str]:
        uid, _gid = self.sandbox.uid_gid()
        return [self.sandbox.interpreter, f"{MOUNT_EVIDENCE}/{PROBE_SCRIPT.name}", "--output", f"{MOUNT_EVIDENCE}/probe-{suffix}.json", "--nonce", nonce, "--expect-network", network, "--expect-uid", str(uid), "--workspace", MOUNT_WORKSPACE, "--", *inner]

    def _assert_workspace_visible(self, workspace: Workspace) -> None:
        base = str(workspace.root.parent.resolve())
        if self._visible_bases.get(base):
            return
        token = uuid.uuid4().hex
        marker = workspace.root / f".fa-mount-probe-{token}"
        marker.write_text(token, encoding="utf-8")
        _chmod_world(marker, 0o644)
        identity = self._image_identity()
        spec = ContainerSpec(
            name=f"fa-probe-{token[:12]}",
            image=identity.reference,
            argv=[self.sandbox.interpreter, "-c", f"import sys; sys.exit(0 if open('{MOUNT_PROBE}/{marker.name}').read().strip() == '{token}' else 3)"],
            user=self.sandbox.resolved_user(),
            network=NETWORK_NONE,
            workdir="/",
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp"},
            seccomp=self.sandbox.seccomp,
            memory_mb=64,
            cpus=self.sandbox.cpus,
            pids_limit=16,
            mounts=[Mount(source=str(workspace.root), target=MOUNT_PROBE, read_only=True)],
            tmpfs={"/tmp": f"{TMPFS_OPTIONS},size=8m"},
            labels={LABEL_OWNER: OWNER, LABEL_PHASE: "mount-probe", LABEL_NONCE: token},
        )
        container_id = self.runtime.create(spec)
        try:
            _out, err, timed_out = self.runtime.start_and_wait(container_id, 60.0)
            state = self.runtime.state(container_id)
        finally:
            self.runtime.remove(container_id)
            try:
                marker.unlink()
            except OSError:
                pass
        if timed_out or state.exit_code != 0:
            raise InfrastructureError(
                f"the daemon cannot read the workspace at {workspace.root} (exit {state.exit_code}; {err.strip()[-200:]}): the daemon runs in a VM that shares only some host paths "
                "(Colima: $HOME and /tmp/colima; Docker Desktop: /Users, /Volumes, /private, /tmp). Use --container-workspace-base with a shared directory."
            )
        self._visible_bases[base] = True

    def _needs_prepare(self, project: Path) -> Tuple[bool, str]:
        if (project / "pyproject.toml").is_file():
            return True, "pyproject.toml"
        present = [name for name in REQUIREMENTS_FILES if (project / name).is_file()]
        if present:
            return True, ", ".join(present)
        return False, "no dependency declaration (pyproject.toml or requirements files): the image's interpreter is used as is"

    def _prepare(self, workspace: Workspace, nonce: str, identity: ImageIdentity, environment: Dict[str, object], needs_runner: bool, created: List[str]) -> Optional[Dict[str, object]]:
        key = str(workspace.root)
        if key in self._prepared:
            environment["phases"]["prepare"] = {"reused": True, **self._prepared[key]}
            return self._prepared[key]
        needed, source = self._needs_prepare(workspace.project)
        if not needed:
            environment["phases"]["prepare"] = {"skipped": True, "reason": source}
            return None
        volume = f"fa-venv-{nonce}"
        self.runtime.volume_create(volume, {LABEL_OWNER: OWNER, LABEL_NONCE: nonce, LABEL_WORKSPACE: workspace.root.name})
        record: Dict[str, object] = {"volume": volume, "source": source, "network": self.sandbox.network_prepare}
        share_cache = bool(self.sandbox.pip_cache_volume) and self.granted_trust in (TRUST_FIXTURE, TRUST_TRUSTED)
        cache_volume = self.sandbox.pip_cache_volume if share_cache else None
        record["pip_cache"] = {"volume": cache_volume, "shared": share_cache, "reason": None if share_cache else ("disabled by policy" if not self.sandbox.pip_cache_volume else "untrusted profile: a shared cache could be poisoned for later runs")}
        if cache_volume and not self.runtime.volume_exists(cache_volume):
            self.runtime.volume_create(cache_volume, {LABEL_OWNER: OWNER, LABEL_PHASE: "pip-cache"})
        # ownership of the fresh volume(s): the image's chown binary as root with CAP_CHOWN only, nothing from the project mounted, no network
        uid, gid = self.sandbox.uid_gid()
        init_mounts = [Mount(source=volume, target=MOUNT_VENV, kind="volume")] + ([Mount(source=cache_volume, target=MOUNT_CACHE, kind="volume")] if cache_volume else [])
        init = ContainerSpec(
            name=f"fa-init-{nonce[:12]}",
            image=identity.reference,
            argv=["chown", f"{uid}:{gid}", MOUNT_VENV] + ([MOUNT_CACHE] if cache_volume else []),
            user="0:0",
            network=NETWORK_NONE,
            workdir="/",
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp"},
            cap_add=("CHOWN",),
            seccomp=self.sandbox.seccomp,
            memory_mb=64,
            cpus=self.sandbox.cpus,
            pids_limit=16,
            mounts=init_mounts,
            tmpfs={"/tmp": f"{TMPFS_OPTIONS},size=8m"},
            labels=self._labels(nonce, "volume-init", workspace),
        )
        init_id = self.runtime.create(init)
        created.append(init_id)
        init_doc = self.runtime.inspect(init_id)
        init_violations = verify_effective(init_doc, init)
        if init_violations:
            raise SecurityRejection("the volume initialisation container does not match its specification: " + "; ".join(init_violations))
        _out, err, timed_out = self.runtime.start_and_wait(init_id, 60.0)
        init_state = self.runtime.state(init_id)
        record["volume_init"] = {"container_id": init_id, "user": "0:0", "capabilities": ["CHOWN"], "command": init.argv, "exit_code": init_state.exit_code, "note": "the image's chown binary on the empty volume(s); no project content, no network"}
        if timed_out or init_state.exit_code != 0:
            raise InfrastructureError(f"could not initialise the prepared-environment volume: {err.strip()[-300:]}")
        # the preparation itself: non-root, read-only root, workspace read-only, network as the policy allows
        prepare_argv = [self.sandbox.interpreter, f"{MOUNT_EVIDENCE}/{PREPARE_SCRIPT.name}", "--workspace", MOUNT_WORKSPACE, "--venv", f"{MOUNT_VENV}/env", "--output", f"{MOUNT_EVIDENCE}/prepare-{nonce}.json", "--nonce", nonce, "--groups", PREPARE_GROUPS]
        prepare_argv += ["--cache-dir", f"{MOUNT_CACHE}/pip"] if cache_volume else ["--no-cache"]
        if needs_runner:
            prepare_argv += ["--ensure", "pytest"]
        spec = self._base_spec(f"fa-prepare-{nonce[:12]}", nonce, "prepare", workspace, identity, self._probe_argv(nonce, f"prepare-{nonce}", self.sandbox.network_prepare, prepare_argv), self.sandbox.network_prepare, self.sandbox.prepare_memory_mb, max(self.sandbox.tmp_size_mb, 512), self.sandbox.prepare_timeout_s)
        spec.env.update({"PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1", "PIP_PROGRESS_BAR": "off"})
        spec.mounts = [Mount(source=str(workspace.project), target=MOUNT_WORKSPACE, read_only=True), Mount(source=str(workspace.root / "evidence"), target=MOUNT_EVIDENCE), Mount(source=volume, target=MOUNT_VENV, kind="volume")]
        if cache_volume:
            spec.mounts.append(Mount(source=cache_volume, target=MOUNT_CACHE, kind="volume"))
        record["spec"] = spec.to_dict()
        container_id = self.runtime.create(spec)
        created.append(container_id)
        document = self.runtime.inspect(container_id)
        violations = verify_effective(document, spec)
        record["daemon_view"] = summarize_effective(document)
        if violations:
            raise SecurityRejection("the preparation container does not match the policy: " + "; ".join(violations))
        started = time.time()
        stdout, stderr, timed_out = self.runtime.start_and_wait(container_id, self.sandbox.prepare_timeout_s)
        state = self.runtime.state(container_id)
        probe = self._read_json(workspace.root / "evidence" / f"probe-prepare-{nonce}.json")
        report = self._read_json(workspace.root / "evidence" / f"prepare-{nonce}.json")
        record.update({"container_id": container_id, "duration_s": round(time.time() - started, 3), "exit_code": state.exit_code, "oom_killed": state.oom_killed, "timed_out": timed_out, "effective": probe, "report": report, "stderr_tail": stderr[-1500:]})
        if state.exit_code == EXIT_PROBE_REJECTED:
            raise SecurityRejection("the in-container probe refused the preparation container: " + "; ".join(str(v) for v in (probe or {}).get("violations", [])))
        if timed_out or state.exit_code != 0 or not report:
            raise InfrastructureError(f"dependency preparation failed (exit {state.exit_code}{', timed out' if timed_out else ''}{', oom' if state.oom_killed else ''}): {(stderr or stdout).strip()[-400:]}")
        self._prepared[key] = record
        environment["phases"]["prepare"] = record
        return record

    def _run_spec(self, workspace: Workspace, nonce: str, identity: ImageIdentity, instrumented: List[str], prepared: Optional[Dict[str, object]]) -> ContainerSpec:
        spec = self._base_spec(f"fa-run-{nonce[:12]}", nonce, "run", workspace, identity, self._probe_argv(nonce, nonce, NETWORK_NONE, instrumented), NETWORK_NONE, self.sandbox.memory_mb, self.sandbox.tmp_size_mb, 600.0)
        spec.mounts = [Mount(source=str(workspace.project), target=MOUNT_WORKSPACE), Mount(source=str(workspace.root / "evidence"), target=MOUNT_EVIDENCE)]
        if prepared and prepared.get("volume"):
            spec.mounts.append(Mount(source=str(prepared["volume"]), target=MOUNT_VENV, read_only=True, kind="volume"))
        return spec

    def _guarantees(self, probe: Optional[Dict[str, object]], document: Dict[str, object], spec: ContainerSpec, prepared: Optional[Dict[str, object]]) -> List[str]:
        probe = probe or {}
        out: List[str] = []
        if probe:
            out.append(f"process ran as uid {probe.get('uid')} gid {probe.get('gid')} (non-root), verified from inside")
            if probe.get("root_readonly"):
                out.append("root filesystem read-only (daemon configuration and an in-container write attempt)")
            if probe.get("cap_bnd") == "0" * 16:
                out.append("capability bounding set empty")
            if probe.get("no_new_privs") == 1:
                out.append("no-new-privileges in force")
            if probe.get("seccomp_mode") == 2:
                out.append(f"seccomp filter active (mode 2, profile {spec.seccomp})")
            if sorted(probe.get("network_interfaces") or []) in (["lo"], []):
                out.append(f"no network interface besides lo; an outbound connection attempt failed ({probe.get('connect_attempt')})")
            if probe.get("memory_max"):
                out.append(f"memory limit {probe.get('memory_max')} bytes (cgroup memory.max), swap equal to memory")
            if probe.get("pids_max"):
                out.append(f"pids limit {probe.get('pids_max')} (cgroup pids.max)")
            if probe.get("cpu_max"):
                out.append(f"cpu quota {probe.get('cpu_max')} (cgroup cpu.max)")
            if probe.get("docker_socket_present") is False:
                out.append("no Docker socket inside the container")
            if not probe.get("home_secrets"):
                out.append("HOME is a tmpfs without credential directories; the environment holds only the keys the executor set")
            if probe.get("tmp_noexec"):
                out.append("/tmp is a noexec, nosuid, nodev tmpfs")
        mounts = [m.target for m in spec.mounts]
        out.append("mounts: " + ", ".join(f"{m.target} ({'ro' if m.read_only else 'rw'}, {m.kind})" for m in spec.mounts) + "; nothing else from the host" if mounts else "no mounts")
        if prepared:
            out.append("dependencies prepared in a separate non-root container on a read-only root; the prepared environment is mounted read-only for the run")
        return out

    def _cleanup(self, created: List[str], environment: Dict[str, object]) -> None:
        cleanup = environment["cleanup"]
        assert isinstance(cleanup, dict)
        for container_id in created:
            try:
                if self.runtime.remove(container_id):
                    cleanup["containers_removed"].append(container_id)
                else:
                    cleanup["leftovers"].append(container_id)
            except InfrastructureError:
                cleanup["leftovers"].append(container_id)
        for record in self._prepared.values():
            if record.get("volume"):
                cleanup["volumes_kept"].append(str(record["volume"]))

    @staticmethod
    def _read_json(path: Path) -> Optional[Dict[str, object]]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- #
# leftovers
# --------------------------------------------------------------------------- #


def reap_leftovers(runtime: ContainerRuntime, *, purge_cache: bool = False, cache_volume: Optional[str] = "fastapi-architect-pip-cache") -> Dict[str, List[str]]:
    """Remove every container and prepared volume the executor labelled (an interrupted run leaves them behind);
    the shared pip cache volume is kept unless ``purge_cache``. Returns what was removed and what resisted."""
    report: Dict[str, List[str]] = {"containers_removed": [], "volumes_removed": [], "failed": []}
    for container_id in runtime.list_labelled("container", LABEL_OWNER, OWNER):
        (report["containers_removed"] if runtime.remove(container_id) else report["failed"]).append(container_id)
    for volume in runtime.list_labelled("volume", LABEL_OWNER, OWNER):
        if volume == cache_volume and not purge_cache:
            continue
        (report["volumes_removed"] if runtime.volume_remove(volume) else report["failed"]).append(volume)
    return report


def host_facts() -> Dict[str, str]:
    return {"python": sys.version.split()[0], "platform": platform.platform()}
