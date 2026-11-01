"""v2.1, increment 1: the container executor.

Two layers. Without a runtime, a ``FakeRuntime`` behind the ``ContainerRuntime`` interface proves the executor's
own behaviour: policy validation, the exact ``docker create`` arguments, the verification of the daemon-reported
configuration, the phases (volume init, preparation, run), the evidence binding, every refusal and failure path,
the cleanup and the reaping of leftovers, and the selection rules (no fallback to the local executor). With
``FA_CONTAINER=1`` and a usable Docker daemon, the same executor runs the self-contained ``container21/sandbox``
fixture whose tests assert the restrictions from inside, the timeout, the memory and pids limits, the refusals,
an infrastructure failure, the leftover cleanup, and the three consumers (design, evolution, remediation).
Nothing here attempts to escape the runtime or touch the host outside the workspace copies.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from _audit_support import FIXTURES, REPO_ROOT, SKILL_SCRIPTS

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

import fastapi_design as design  # noqa: E402
from fastapi_audit.remediate import container_cli  # noqa: E402
from fastapi_audit.remediate import containers as C  # noqa: E402
from fastapi_audit.remediate.containers import (  # noqa: E402
    ContainerExecutor,
    ContainerRuntime,
    ContainerSpec,
    ContainerState,
    DockerCliRuntime,
    ImageIdentity,
    ImagePolicy,
    InfrastructureError,
    Mount,
    RuntimeInfo,
    SandboxPolicy,
    SecurityRejection,
    reap_leftovers,
    verify_effective,
)
from fastapi_audit.remediate.executors import REGISTRY, Authorization, LocalProcessExecutor, select_executor  # noqa: E402
from fastapi_audit.remediate.isolation import ExecutionPolicy, Workspace  # noqa: E402
from fastapi_audit.remediate.model import TestRun  # noqa: E402
from fastapi_audit.remediate.pipeline import RUN_SCHEMA, RunOptions, run_remediation  # noqa: E402
from fastapi_audit.remediate.runners import PytestJUnitAdapter, UnittestJsonAdapter  # noqa: E402
from fastapi_design.verify import run_generated_tests  # noqa: E402

SANDBOX = FIXTURES / "container21" / "sandbox"
REMEDIATION19 = FIXTURES / "remediation19"
EXAMPLES = REPO_ROOT / "plugins" / "fastapi-architect" / "examples"
PY = sys.executable
DOCKER_WANTED = os.environ.get("FA_CONTAINER") == "1" and shutil.which("docker") is not None
PROBE_OK = {"uid": 65534, "gid": 65534, "euid": 65534, "root_readonly": True, "cap_bnd": "0" * 16, "cap_eff": "0" * 16, "no_new_privs": 1, "seccomp_mode": 2, "network_interfaces": ["lo"], "connect_attempt": "ENETUNREACH", "memory_max": str(512 * C.MIB), "pids_max": "256", "cpu_max": "100000 100000", "docker_socket_present": False, "home": "/tmp", "home_secrets": [], "env_keys": ["HOME", "PATH"], "suspicious_env_keys": [], "tmp": {"writable": True, "errno": None}, "tmp_noexec": True, "workspace": {"writable": True, "errno": None}, "violations": []}


def inspect_document(spec: ContainerSpec) -> Dict[str, object]:
    """What a daemon reports for a container created exactly as specified."""
    mounts = []
    for m in spec.mounts:
        entry = {"Type": m.kind, "Destination": m.target, "RW": not m.read_only, "Mode": "ro" if m.read_only else "rw"}
        if m.kind == "bind":
            entry["Source"] = str(Path(m.source).resolve())
        else:
            entry["Name"] = m.source
            entry["Source"] = f"/var/lib/docker/volumes/{m.source}/_data"
        mounts.append(entry)
    return {
        "Config": {"User": spec.user, "Env": [f"{k}={v}" for k, v in spec.env.items()]},
        "HostConfig": {
            "ReadonlyRootfs": spec.read_only_root,
            "Privileged": False,
            "CapDrop": ["ALL"] if spec.drop_all_capabilities else [],
            "CapAdd": list(spec.cap_add) or None,
            "SecurityOpt": (["no-new-privileges"] if spec.no_new_privileges else []) + ([f"seccomp={spec.seccomp}"] if spec.seccomp else []),
            "NetworkMode": spec.network,
            "Memory": spec.memory_mb * C.MIB,
            "MemorySwap": spec.memory_mb * C.MIB,
            "NanoCpus": int(round(spec.cpus * 1e9)),
            "PidsLimit": spec.pids_limit,
            "Devices": [],
            "Tmpfs": dict(spec.tmpfs),
            "IpcMode": "private",
            "CgroupnsMode": "private",
            "UsernsMode": "",
            "PidMode": "",
            "UTSMode": "",
            "Ulimits": [{"Name": k, "Soft": v, "Hard": v} for k, v in spec.ulimits.items()],
        },
        "Mounts": mounts,
        "State": {"Status": "created", "ExitCode": 0, "OOMKilled": False, "Error": ""},
    }


class FakeRuntime(ContainerRuntime):
    """Deterministic stand-in for a daemon: records every call, writes the files a real run would leave in the
    evidence mount, and simulates the outcomes the tests ask for."""

    name = "fake"

    def __init__(self, run_behaviour: str = "pass", info_error: Optional[str] = None) -> None:
        self.run_behaviour = run_behaviour
        self.info_error = info_error
        self.tamper = None  # callable(document, spec) -> document
        self.fail_phase: Optional[str] = None  # raise InfrastructureError when this phase starts
        self.specs: Dict[str, ContainerSpec] = {}
        self.created: List[str] = []
        self.started: List[Tuple[str, str]] = []
        self.removed: List[str] = []
        self.states: Dict[str, ContainerState] = {}
        self.volumes: Dict[str, Dict[str, str]] = {}
        self.pulled: List[str] = []

    def info(self) -> RuntimeInfo:
        if self.info_error:
            raise InfrastructureError(self.info_error)
        return RuntimeInfo(name=self.name, available=True, client_version="0.fake", server_version="0.fake", operating_system="fake os", kernel="0.0", architecture="test", cgroup_version="2", security_options=["name=seccomp,profile=builtin"], rootless=False, endpoint="fake")

    def ensure_image(self, policy: ImagePolicy) -> ImageIdentity:
        errors = policy.validate()
        if errors:
            raise SecurityRejection("; ".join(errors))
        self.pulled.append(policy.pinned_reference)
        digest = policy.digest or ("sha256:" + "a" * 64)
        return ImageIdentity(reference=policy.pinned_reference, image_id="sha256:" + "b" * 64, repo_digests=[f"python@{digest}"], pinned=bool(policy.digest), platform="linux/test")

    def create(self, spec: ContainerSpec) -> str:
        container_id = f"c{len(self.created) + 1:02d}{spec.labels.get(C.LABEL_PHASE, '')}"
        self.specs[container_id] = spec
        self.created.append(container_id)
        self.states[container_id] = ContainerState(container_id=container_id, exit_code=None, status="created")
        return container_id

    def inspect(self, container_id: str) -> Dict[str, object]:
        spec = self.specs[container_id]
        document = inspect_document(spec)
        if self.tamper is not None:
            document = self.tamper(document, spec)
        state = self.states[container_id]
        document["State"] = {"Status": state.status, "ExitCode": state.exit_code, "OOMKilled": state.oom_killed, "Error": state.error}
        return document

    def _host_evidence(self, spec: ContainerSpec) -> Optional[Path]:
        for m in spec.mounts:
            if m.target == C.MOUNT_EVIDENCE:
                return Path(m.source)
        return None

    def start_and_wait(self, container_id: str, timeout_s: float) -> Tuple[str, str, bool]:
        spec = self.specs[container_id]
        phase = spec.labels.get(C.LABEL_PHASE, "")
        self.started.append((container_id, phase))
        if self.fail_phase == phase:
            raise InfrastructureError(f"simulated daemon failure during {phase}")
        state = self.states[container_id]
        evidence = self._host_evidence(spec)
        nonce = spec.labels.get(C.LABEL_NONCE, "")
        if phase in ("mount-probe", "volume-init"):
            state.exit_code, state.status = 0, "exited"
            return "", "", False
        if phase == "prepare":
            assert evidence is not None
            (evidence / f"probe-prepare-{nonce}.json").write_text(json.dumps({**PROBE_OK, "network_interfaces": ["eth0", "lo"], "connect_attempt": "connected"}), encoding="utf-8")
            (evidence / f"prepare-{nonce}.json").write_text(json.dumps({"nonce": nonce, "source": "fake", "installed": ["fastapi==0.1"], "installed_count": 1, "returncode": 0, "steps": []}), encoding="utf-8")
            state.exit_code, state.status = 0, "exited"
            return "prepared\n", "", False
        assert phase == "run" and evidence is not None
        behaviour = self.run_behaviour
        probe = dict(PROBE_OK)
        probe["memory_max"] = str(spec.memory_mb * C.MIB)
        probe["pids_max"] = str(spec.pids_limit)
        if behaviour == "probe_rejected":
            probe["violations"] = ["running as root"]
            (evidence / f"probe-{nonce}.json").write_text(json.dumps(probe), encoding="utf-8")
            state.exit_code, state.status = C.EXIT_PROBE_REJECTED, "exited"
            return "", "sandbox probe: refusing to run the command: running as root\n", False
        (evidence / f"probe-{nonce}.json").write_text(json.dumps(probe), encoding="utf-8")
        if behaviour == "timeout":
            state.exit_code, state.status = 137, "exited"
            return "partial output", "", True
        if behaviour == "oom":
            state.exit_code, state.status, state.oom_killed = 137, "exited", True
            return "", "Killed", False
        if behaviour == "command_not_found":
            state.exit_code, state.status = C.EXIT_COMMAND_NOT_FOUND, "exited"
            return "", "sandbox probe: cannot execute 'nope'", False
        argv = spec.argv[spec.argv.index("--") + 1:] if "--" in spec.argv else list(spec.argv)  # the command behind the probe
        failing = behaviour == "fail"
        if any(t.startswith("--junit-xml=") for t in argv):
            report = evidence / Path(next(t for t in argv if t.startswith("--junit-xml=")).split("=", 1)[1]).name
            cases = '<testcase classname="t" name="a"/><testcase classname="t" name="b">' + ('<failure message="boom"/>' if failing else "") + "</testcase>"
            report.write_text(f'<?xml version="1.0"?><testsuites><testsuite name="{nonce}" tests="2" failures="{1 if failing else 0}">{cases}</testsuite></testsuites>', encoding="utf-8")
        elif "--output" in argv:
            report = evidence / Path(argv[argv.index("--output") + 1]).name
            counts = {"discovered": 2, "selected": 2, "executed": 2, "passed": 1 if failing else 2, "failed": 1 if failing else 0, "errors": 0, "skipped": 0, "collection_errors": 0}
            tests = {"passed": ["t.a"] + ([] if failing else ["t.b"]), "failed": ["t.b"] if failing else [], "errors": [], "skipped": [], "collection_errors": []}
            report.write_text(json.dumps({"nonce": nonce, "counts": counts, "tests": tests}), encoding="utf-8")
        if behaviour == "no_report":
            for leftover in evidence.glob(f"*{nonce}.xml"):
                leftover.unlink()
            for leftover in evidence.glob(f"unittest-{nonce}.json"):
                leftover.unlink()
        state.exit_code, state.status = (1 if failing else 0), "exited"
        return "ran\n", "", False

    def state(self, container_id: str) -> ContainerState:
        return self.states[container_id]

    def remove(self, container_id: str) -> bool:
        self.removed.append(container_id)
        return True

    def volume_create(self, name: str, labels: Dict[str, str]) -> None:
        self.volumes[name] = dict(labels)

    def volume_exists(self, name: str) -> bool:
        return name in self.volumes

    def volume_remove(self, name: str) -> bool:
        return self.volumes.pop(name, None) is not None

    def list_labelled(self, kind: str, label: str, value: str) -> List[str]:
        if kind == "container":
            return [cid for cid in self.created if cid not in self.removed and self.specs[cid].labels.get(label) == value]
        return [name for name, labels in self.volumes.items() if labels.get(label) == value]


class FakeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fa-container-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.base = self.tmp / "workspaces"

    def executor(self, runtime: Optional[FakeRuntime] = None, **sandbox) -> ContainerExecutor:
        return ContainerExecutor(runtime=runtime or FakeRuntime(), sandbox=SandboxPolicy(workspace_base=str(self.base), **sandbox), image=ImagePolicy())

    def workspace(self, executor: ContainerExecutor, source: Path = SANDBOX) -> Workspace:
        workspace = Workspace.create(str(source), executor.workspace_dir())
        self.addCleanup(workspace.cleanup)
        return workspace

    def policy(self, command: str, **kwargs) -> ExecutionPolicy:
        return ExecutionPolicy(authorized=True, command=command, timeout_s=kwargs.pop("timeout_s", 60.0), **kwargs)

    def pyproject_project(self, name: str = "pyproject_project") -> Path:
        project = self.tmp / name
        (project / "tests").mkdir(parents=True)
        (project / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0.1"\ndependencies = ["fastapi>=0.115"]\n\n[dependency-groups]\ndev = ["pytest>=8"]\n', encoding="utf-8")
        (project / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
        return project


# --------------------------------------------------------------------------- #
# policies, arguments, verification
# --------------------------------------------------------------------------- #


class PolicyTests(unittest.TestCase):
    def test_weakened_restrictions_are_refused(self) -> None:
        self.assertEqual(SandboxPolicy().validate(), [])
        for weakened, fragment in (
            (SandboxPolicy(user="0:0"), "root"),
            (SandboxPolicy(user="root"), "numeric"),
            (SandboxPolicy(user="1000:0"), "root"),
            (SandboxPolicy(read_only_root=False), "read_only_root"),
            (SandboxPolicy(drop_all_capabilities=False), "drop_all_capabilities"),
            (SandboxPolicy(no_new_privileges=False), "no_new_privileges"),
            (SandboxPolicy(seccomp="unconfined"), "seccomp"),
            (SandboxPolicy(network_run="bridge"), "network_run"),
            (SandboxPolicy(network_prepare="host"), "network_prepare"),
            (SandboxPolicy(memory_mb=0), "memory"),
            (SandboxPolicy(pids_limit=0), "pids"),
            (SandboxPolicy(privileged=True), "privileged"),
            (SandboxPolicy(extra_mounts=("/etc:/etc",)), "mounts"),
        ):
            with self.subTest(fragment=fragment):
                errors = weakened.validate()
                self.assertTrue(errors)
                self.assertTrue(any(fragment in e for e in errors), errors)

    def test_images_need_the_allow_list_or_a_digest(self) -> None:
        self.assertEqual(ImagePolicy().validate(), [])
        self.assertTrue(ImagePolicy(reference="alpine:3.20").validate())
        self.assertEqual(ImagePolicy(reference="ghcr.io/acme/python:3.13", digest="sha256:" + "c" * 64).validate(), [])
        self.assertEqual(ImagePolicy(reference="ghcr.io/acme/python:3.13", digest="sha256:" + "c" * 64).pinned_reference, "ghcr.io/acme/python@sha256:" + "c" * 64)
        self.assertTrue(ImagePolicy(digest="sha256:short").validate())
        self.assertTrue(ImagePolicy(reference="python:3.13-slim extra").validate())

    def test_create_argv_carries_every_hardening_flag_and_only_the_specified_mounts(self) -> None:
        spec = ContainerSpec(name="fa-run-x", image="python@sha256:" + "a" * 64, argv=["pytest", "-q"], user="65534:65534", network="none", env={"PATH": C.SANDBOX_PATH, "HOME": "/tmp"}, mounts=[Mount("/host/ws/project", C.MOUNT_WORKSPACE), Mount("/host/ws/evidence", C.MOUNT_EVIDENCE), Mount("fa-venv-x", C.MOUNT_VENV, read_only=True, kind="volume")], tmpfs={"/tmp": f"{C.TMPFS_OPTIONS},size=256m"}, labels={C.LABEL_OWNER: C.OWNER}, ulimits={"nofile": "4096:4096"})
        argv = DockerCliRuntime(binary="docker").create_argv(spec)
        text = " ".join(argv)
        for fragment in ("--user 65534:65534", "--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges", "--security-opt seccomp=builtin", "--memory 512m", "--memory-swap 512m", "--pids-limit 256", "--cpus 1.0", "--ipc private", "--tmpfs /tmp:rw,noexec,nosuid,nodev,size=256m", "type=bind,source=/host/ws/project,target=/workspace", "type=bind,source=/host/ws/evidence,target=/evidence", "type=volume,source=fa-venv-x,target=/venv,readonly", "--label fastapi-architect.owner=container-executor", "--ulimit nofile=4096:4096", "--env HOME=/tmp"):
            self.assertIn(fragment, text)
        self.assertNotIn("docker.sock", text)
        self.assertNotIn("--privileged", text)
        self.assertNotIn("--cap-add", text)
        self.assertEqual(argv[-3:], ["python@sha256:" + "a" * 64, "pytest", "-q"])
        self.assertEqual(argv.count("--mount"), 3)

    def test_verify_effective_accepts_the_faithful_document_and_names_each_weakening(self) -> None:
        spec = ContainerSpec(name="x", image="img", argv=["python"], user="65534:65534", network="none", mounts=[Mount(str(Path.cwd()), C.MOUNT_WORKSPACE), Mount("fa-venv-x", C.MOUNT_VENV, read_only=True, kind="volume")], tmpfs={"/tmp": f"{C.TMPFS_OPTIONS},size=16m"})
        self.assertEqual(verify_effective(inspect_document(spec), spec), [])

        def mutated(**changes):
            document = inspect_document(spec)
            for key, value in changes.items():
                section, name = key.split(".", 1)
                if section == "Mounts":
                    document["Mounts"] = value
                else:
                    document[section][name] = value
            return document

        home = str(Path.home().resolve())
        cases = {
            "root user": mutated(**{"Config.User": "0:0"}),
            "writable root": mutated(**{"HostConfig.ReadonlyRootfs": False}),
            "privileged": mutated(**{"HostConfig.Privileged": True}),
            "capabilities kept": mutated(**{"HostConfig.CapDrop": []}),
            "capability added": mutated(**{"HostConfig.CapAdd": ["SYS_ADMIN"]}),
            "no-new-privileges missing": mutated(**{"HostConfig.SecurityOpt": ["seccomp=builtin"]}),
            "seccomp unconfined": mutated(**{"HostConfig.SecurityOpt": ["no-new-privileges", "seccomp=unconfined"]}),
            "network bridge": mutated(**{"HostConfig.NetworkMode": "bridge"}),
            "memory unlimited": mutated(**{"HostConfig.Memory": 0}),
            "swap allowed": mutated(**{"HostConfig.MemorySwap": -1}),
            "pids unlimited": mutated(**{"HostConfig.PidsLimit": None}),
            "cpu unlimited": mutated(**{"HostConfig.NanoCpus": 0}),
            "device exposed": mutated(**{"HostConfig.Devices": [{"PathOnHost": "/dev/sda"}]}),
            "host pid namespace": mutated(**{"HostConfig.PidMode": "host"}),
            "tmpfs exec": mutated(**{"HostConfig.Tmpfs": {"/tmp": "rw,size=16m"}}),
            "docker socket mounted": mutated(**{"Mounts.x": inspect_document(spec)["Mounts"] + [{"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock", "RW": True}]}),
            "ssh keys mounted": mutated(**{"Mounts.x": inspect_document(spec)["Mounts"] + [{"Type": "bind", "Source": f"{home}/.ssh", "Destination": "/keys", "RW": False}]}),
            "home mounted": mutated(**{"Mounts.x": inspect_document(spec)["Mounts"] + [{"Type": "bind", "Source": home, "Destination": "/home/x", "RW": False}]}),
            "environment writable": mutated(**{"Mounts.x": [dict(m, RW=True) for m in inspect_document(spec)["Mounts"]]}),
            "mount missing": mutated(**{"Mounts.x": inspect_document(spec)["Mounts"][:1]}),
            "unknown volume": mutated(**{"Mounts.x": inspect_document(spec)["Mounts"] + [{"Type": "volume", "Name": "other", "Destination": "/other", "RW": True}]}),
        }
        for label, document in cases.items():
            with self.subTest(case=label):
                self.assertTrue(verify_effective(document, spec), label)

    def test_forbidden_sources_include_the_socket_the_home_and_its_credentials(self) -> None:
        exact, prefixes = C.forbidden_mount_sources()
        self.assertIn(str(Path.home().resolve()), exact)
        self.assertIn(str(Path.home().resolve() / ".ssh"), prefixes)
        for source in ("/var/run/docker.sock", "/run/docker.sock", "/etc", "/etc/passwd", str(Path.home()), str(Path.home() / ".aws" / "credentials"), "/"):
            self.assertTrue(C.source_is_forbidden(source), source)
        for source in (str(Path.home() / ".cache" / "fastapi-architect" / "workspaces" / "ws-1" / "project"), str(Path.cwd())):
            self.assertFalse(C.source_is_forbidden(source), source)

    def test_selection_never_falls_back_and_the_local_executor_still_refuses_untrusted(self) -> None:
        root = str(SANDBOX)
        self.assertIn("container", REGISTRY)
        executor, denial, code = select_executor(Authorization(execution=True, trust="untrusted", scope=root), root, name="container")
        self.assertEqual((executor.name, executor.isolation_level, code), ("container", "isolated", ""), denial)
        _, denial, code = select_executor(Authorization(execution=True, trust="untrusted", scope=root), root, name="local")
        self.assertEqual(code, "executor_unavailable")
        self.assertIn("--executor container", denial)
        self.assertEqual(select_executor(Authorization(execution=True, trust="untrusted", scope=root), root, name="podman")[2], "executor_unavailable")
        self.assertEqual(select_executor(Authorization(execution=True, trust="untrusted", scope=str(FIXTURES / "sa_sessions")), root, name="container")[2], "scope_mismatch")
        self.assertIsNone(LocalProcessExecutor().workspace_dir())
        self.assertTrue(ContainerExecutor.not_provided)
        self.assertTrue(any("kernel" in item for item in ContainerExecutor.not_provided))

    def test_adapters_translate_report_paths_to_the_mount_point(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Path(tmp)
            argv, report = PytestJUnitAdapter().instrument(["pytest", "-q"], evidence, "n0nce", mount_point=Path("/evidence"))
            self.assertIn("--junit-xml=/evidence/pytest-n0nce.xml", argv)
            self.assertEqual(report, evidence / "pytest-n0nce.xml")
            argv, report = UnittestJsonAdapter().instrument(["python", "-m", "unittest", "tests.x"], evidence, "n0nce", mount_point=Path("/evidence"))
            self.assertEqual(argv[:4], ["python", "/evidence/fa_unittest_runner.py", "--output", "/evidence/unittest-n0nce.json"])
            self.assertEqual(report, evidence / "unittest-n0nce.json")
            self.assertTrue((evidence / "fa_unittest_runner.py").is_file())
            argv, _report = PytestJUnitAdapter().instrument(["pytest"], evidence, "n0nce")
            self.assertIn(f"--junit-xml={evidence / 'pytest-n0nce.xml'}", argv)

    def test_schemas_and_records_are_extended_additively(self) -> None:
        self.assertEqual(RUN_SCHEMA, "remediation_run/1.3")
        self.assertEqual(design.REPORT_SCHEMA, "architecture_report/1.1")
        from fastapi_design.evolution import REPORT_SCHEMA as EVOLUTION_SCHEMA

        self.assertEqual(EVOLUTION_SCHEMA, "evolution_report/1.4")
        record = TestRun().to_dict()
        self.assertIn("environment", record)
        self.assertEqual(record["environment"], {})
        for key in ("ran", "command", "executor", "returncode", "outcome", "runner", "counts", "evidence", "stdout_tail", "stderr_tail"):
            self.assertIn(key, record)

    def test_cli_flags_configure_the_registered_container_executor(self) -> None:
        parser = argparse.ArgumentParser()
        container_cli.add_container_arguments(parser, reap=True)
        args = parser.parse_args(["--container-memory-mb", "128", "--container-pids", "32", "--container-user", "1000:1000", "--container-prepare-network", "none", "--container-no-pip-cache", "--container-digest", "sha256:" + "d" * 64, "--container-image", "python:3.12-slim"])
        executor = container_cli.container_executor_from_args(args)
        try:
            self.assertIs(REGISTRY["container"], executor)
            self.assertEqual((executor.sandbox.memory_mb, executor.sandbox.pids_limit, executor.sandbox.resolved_user(), executor.sandbox.network_prepare, executor.sandbox.pip_cache_volume), (128, 32, "1000:1000", "none", None))
            self.assertEqual(executor.image.pinned_reference, "python@sha256:" + "d" * 64)
            self.assertIsNone(container_cli.maybe_reap(parser.parse_args([]), "x"))
        finally:
            REGISTRY["container"] = ContainerExecutor()
        for script in ("audit_remediate.py", "design_fastapi.py"):
            proc = subprocess.run([PY, str(SKILL_SCRIPTS / script), "--help"], capture_output=True, text=True, timeout=120)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("--container-image", proc.stdout)
        self.assertIn("--container-reap", subprocess.run([PY, str(SKILL_SCRIPTS / "audit_remediate.py"), "--help"], capture_output=True, text=True, timeout=120).stdout)


# --------------------------------------------------------------------------- #
# the executor against the fake runtime
# --------------------------------------------------------------------------- #


class FakeRuntimeExecutorTests(FakeCase):
    def test_a_passing_run_is_verified_from_the_bound_report_and_records_the_environment(self) -> None:
        runtime = FakeRuntime()
        executor = self.executor(runtime)
        self.assertIsNone(executor.accepts("untrusted"))
        workspace = self.workspace(executor)
        run = executor.run(f"{PY} -m unittest tests.test_controls", workspace, self.policy(f"{PY} -m unittest tests.test_controls"))
        self.assertEqual((run.ran, run.outcome, run.passed, run.executor, run.runner), (True, "passed", True, "container", "unittest-json"))
        self.assertTrue(run.verifiable)
        self.assertEqual(run.counts["passed"], 2)
        env = run.environment
        self.assertEqual(env["executor"], "container")
        self.assertEqual(env["isolation_level"], "isolated")
        self.assertEqual(env["trust_profile"], "untrusted")
        self.assertEqual(env["runtime"]["name"], "fake")
        self.assertIn("trust_boundary", env["runtime"])
        self.assertTrue(env["image"]["repo_digests"])
        self.assertFalse(env["image"]["pinned_by_digest"])
        self.assertTrue(env["phases"]["prepare"]["skipped"])
        run_phase = env["phases"]["run"]
        self.assertEqual(run_phase["termination"], "exited")
        self.assertEqual(run_phase["spec"]["network"], "none")
        self.assertEqual(run_phase["spec"]["user"], "65534:65534")
        self.assertEqual([m["target"] for m in run_phase["spec"]["mounts"]], ["/workspace", "/evidence"])
        self.assertEqual(run_phase["daemon_view"]["network_mode"], "none")
        self.assertEqual(env["effective"]["uid"], 65534)
        self.assertTrue(any("non-root" in g for g in env["guarantees_applied"]))
        self.assertTrue(any("seccomp" in g for g in env["guarantees_applied"]))
        self.assertTrue(env["limitations"])
        self.assertIsNone(env["rejection"])
        self.assertEqual(env["cleanup"]["containers_removed"], [cid for cid in runtime.created if cid.endswith("run")])
        self.assertEqual(env["cleanup"]["leftovers"], [])
        # the instrumented command sees the evidence through the mount point; the host reads it from the workspace
        self.assertIn("/evidence/fa_unittest_runner.py", run.instrumented_command)
        self.assertTrue((workspace.root / "evidence" / "fa_unittest_runner.py").is_file())
        self.assertTrue((workspace.root / "evidence" / C.PROBE_SCRIPT.name).is_file())
        spec = runtime.specs[[cid for cid in runtime.created if cid.endswith("run")][0]]
        self.assertEqual(spec.argv[:2], [C.IMAGE_INTERPRETER, f"/evidence/{C.PROBE_SCRIPT.name}"])
        self.assertIn("--expect-network", spec.argv)
        self.assertEqual(spec.env["PATH"], C.SANDBOX_PATH)
        self.assertNotIn("DOCKER_HOST", spec.env)
        # the mount probe ran once, read-only, and was removed
        probes = [cid for cid in runtime.created if cid.endswith("mount-probe")]
        self.assertEqual(len(probes), 1)
        self.assertTrue(runtime.specs[probes[0]].mounts[0].read_only)
        self.assertIn(probes[0], runtime.removed)
        # a second run on the same base does not probe the mount again
        executor.run(f"{PY} -m unittest tests.test_controls", workspace, self.policy(f"{PY} -m unittest tests.test_controls"))
        self.assertEqual(len([cid for cid in runtime.created if cid.endswith("mount-probe")]), 1)

    def test_a_project_with_dependencies_is_prepared_in_its_own_container(self) -> None:
        runtime = FakeRuntime()
        executor = self.executor(runtime)
        executor.accepts("untrusted")
        workspace = self.workspace(executor, self.pyproject_project())
        run = executor.run("pytest -q", workspace, self.policy("pytest -q"))
        self.assertEqual((run.outcome, run.runner), ("passed", "pytest-junit"))
        phases = run.environment["phases"]
        prepare = phases["prepare"]
        self.assertEqual(prepare["network"], "bridge")
        self.assertEqual(prepare["source"], "pyproject.toml")
        self.assertFalse(prepare["pip_cache"]["shared"], prepare["pip_cache"])
        init = prepare["volume_init"]
        self.assertEqual((init["user"], init["capabilities"], init["exit_code"]), ("0:0", ["CHOWN"], 0))
        self.assertEqual(init["command"][:2], ["chown", "65534:65534"])
        prepare_spec = prepare["spec"]
        self.assertEqual(prepare_spec["user"], "65534:65534")
        self.assertEqual({m["target"]: m["read_only"] for m in prepare_spec["mounts"]}, {"/workspace": True, "/evidence": False, "/venv": False})
        self.assertIn("--ensure", prepare_spec["argv"])
        self.assertEqual(prepare["report"]["installed_count"], 1)
        run_spec = phases["run"]["spec"]
        self.assertEqual({m["target"]: m["read_only"] for m in run_spec["mounts"]}, {"/workspace": False, "/evidence": False, "/venv": True})
        self.assertEqual(run_spec["network"], "none")
        self.assertTrue(any(t.startswith("--junit-xml=/evidence/pytest-") for t in run_spec["argv"]))
        self.assertEqual(run.environment["cleanup"]["volumes_kept"], [prepare["volume"]])
        self.assertIn(prepare["volume"], runtime.volumes)
        phases_order = [phase for _cid, phase in runtime.started]
        self.assertEqual(phases_order, ["mount-probe", "volume-init", "prepare", "run"])
        # the prepared environment is reused for a second run on the same workspace, then released
        again = executor.run("pytest -q", workspace, self.policy("pytest -q"))
        self.assertTrue(again.environment["phases"]["prepare"]["reused"])
        executor.release(workspace)
        self.assertNotIn(prepare["volume"], runtime.volumes)
        # fixture and trusted profiles share the pip cache; untrusted never does
        executor.accepts("fixture")
        other = self.workspace(executor, self.pyproject_project("second_project"))
        cached = executor.run("pytest -q", other, self.policy("pytest -q"))
        self.assertTrue(cached.environment["phases"]["prepare"]["pip_cache"]["shared"])
        self.assertIn("fastapi-architect-pip-cache", runtime.volumes)
        self.assertIn("/cache", {m["target"] for m in cached.environment["phases"]["prepare"]["spec"]["mounts"]})
        self.assertNotIn("/cache", {m["target"] for m in cached.environment["phases"]["run"]["spec"]["mounts"]})

    def test_refusals_happen_before_anything_starts(self) -> None:
        cases = {
            "unauthorized image": (FakeRuntime(), dict(image=ImagePolicy(reference="alpine:3.20")), "allow-list"),
            "weakened policy": (FakeRuntime(), dict(sandbox=SandboxPolicy(workspace_base=str(self.base), user="0:0")), "root"),
            "unsupported program": (FakeRuntime(), {}, "not available in the sandbox"),
        }
        for label, (runtime, kwargs, fragment) in cases.items():
            with self.subTest(case=label):
                executor = ContainerExecutor(runtime=runtime, sandbox=kwargs.get("sandbox", SandboxPolicy(workspace_base=str(self.base))), image=kwargs.get("image", ImagePolicy()))
                workspace = self.workspace(executor)
                command = "uv run pytest" if label == "unsupported program" else "pytest -q"
                run = executor.run(command, workspace, self.policy(command))
                self.assertEqual((run.ran, run.outcome), (False, "refused"), label)
                self.assertIn(fragment, run.refused or "")
                self.assertEqual(runtime.started, [], label)
                if label != "unsupported program":
                    self.assertEqual(run.environment["rejection"]["kind"], "security")

    def test_a_tampered_daemon_configuration_is_refused_and_the_container_never_starts(self) -> None:
        runtime = FakeRuntime()

        def drop_no_new_privileges(document, spec):
            if spec.labels.get(C.LABEL_PHASE) == "run":
                document["HostConfig"]["SecurityOpt"] = ["seccomp=builtin"]
            return document

        runtime.tamper = drop_no_new_privileges
        executor = self.executor(runtime)
        workspace = self.workspace(executor)
        run = executor.run(f"{PY} -m unittest tests.test_controls", workspace, self.policy(f"{PY} -m unittest tests.test_controls"))
        self.assertEqual((run.ran, run.outcome), (False, "refused"))
        self.assertIn("no-new-privileges", run.refused)
        self.assertEqual([phase for _cid, phase in runtime.started], ["mount-probe"])
        run_ids = [cid for cid in runtime.created if cid.endswith("run")]
        self.assertEqual(len(run_ids), 1)
        self.assertIn(run_ids[0], runtime.removed)
        self.assertEqual(run.environment["rejection"]["kind"], "security")

    def test_the_in_container_probe_can_refuse_the_command(self) -> None:
        runtime = FakeRuntime(run_behaviour="probe_rejected")
        executor = self.executor(runtime)
        workspace = self.workspace(executor)
        run = executor.run(f"{PY} -m unittest tests.test_controls", workspace, self.policy(f"{PY} -m unittest tests.test_controls"))
        self.assertEqual((run.ran, run.outcome), (False, "refused"))
        self.assertIn("running as root", run.refused)
        self.assertEqual(run.environment["phases"]["run"]["termination"], "probe_rejected")
        self.assertEqual(run.environment["effective"]["violations"], ["running as root"])

    def test_infrastructure_failures_are_inconclusive_never_verified(self) -> None:
        down = self.executor(FakeRuntime(info_error="docker daemon not usable: cannot connect"))
        workspace = self.workspace(down)
        run = down.run("pytest -q", workspace, self.policy("pytest -q"))
        self.assertEqual((run.ran, run.outcome, run.passed), (False, "infrastructure_error", None))
        self.assertIn("cannot connect", run.stderr_tail)
        self.assertEqual(run.environment["rejection"]["kind"], "infrastructure")
        crashing = FakeRuntime()
        crashing.fail_phase = "run"
        executor = self.executor(crashing)
        workspace = self.workspace(executor)
        run = executor.run("pytest -q", workspace, self.policy("pytest -q"))
        self.assertEqual((run.ran, run.outcome), (False, "infrastructure_error"))
        self.assertIn("simulated daemon failure", run.stderr_tail)
        self.assertTrue(all(cid in crashing.removed for cid in crashing.created if cid.endswith("run")), "the container created before the crash is removed")
        missing = FakeRuntime(run_behaviour="command_not_found")
        executor = self.executor(missing)
        workspace = self.workspace(executor)
        run = executor.run("pytest -q", workspace, self.policy("pytest -q"))
        self.assertEqual(run.outcome, "infrastructure_error")
        # through the design seam: nothing ran, the reason is explained, the workspace is released
        REGISTRY["container"] = self.executor(FakeRuntime(info_error="no daemon"))
        try:
            outcome = run_generated_tests(str(SANDBOX), Authorization(execution=True, trust="untrusted", scope=str(FIXTURES)), self.policy(f"{PY} -m unittest tests.test_controls"), f"{PY} -m unittest tests.test_controls", executor_name="container")
        finally:
            REGISTRY["container"] = ContainerExecutor()
        self.assertFalse(outcome["ran"])
        self.assertIn("no daemon", outcome["denied_reason"])
        self.assertEqual(outcome["run"]["outcome"], "infrastructure_error")
        self.assertEqual(outcome["executor"]["name"], "container")

    def test_timeout_oom_failure_and_missing_report_never_pass(self) -> None:
        expectations = {"timeout": ("timeout", "timeout"), "oom": ("infrastructure_error", "oom_killed"), "fail": ("failed", "exited"), "no_report": ("no_evidence", "exited")}
        for behaviour, (outcome, termination) in expectations.items():
            with self.subTest(behaviour=behaviour):
                runtime = FakeRuntime(run_behaviour=behaviour)
                executor = self.executor(runtime)
                workspace = self.workspace(executor)
                run = executor.run("pytest -q", workspace, self.policy("pytest -q", timeout_s=5.0))
                self.assertEqual((run.ran, run.outcome, run.passed), (True, outcome, False))
                self.assertEqual(run.environment["phases"]["run"]["termination"], termination)
                self.assertEqual(run.timed_out, behaviour == "timeout")
                self.assertTrue(all(cid in runtime.removed for cid in runtime.created if cid.endswith("run")))

    def test_leftovers_of_an_interrupted_run_are_reaped(self) -> None:
        runtime = FakeRuntime()
        runtime.create(ContainerSpec(name="fa-run-orphan", image="img", argv=["python"], user="65534:65534", network="none", labels={C.LABEL_OWNER: C.OWNER, C.LABEL_PHASE: "run"}))
        runtime.volume_create("fa-venv-orphan", {C.LABEL_OWNER: C.OWNER})
        runtime.volume_create("fastapi-architect-pip-cache", {C.LABEL_OWNER: C.OWNER, C.LABEL_PHASE: "pip-cache"})
        runtime.volume_create("unrelated", {})
        report = reap_leftovers(runtime)
        self.assertEqual(report["containers_removed"], ["c01run"])
        self.assertEqual(report["volumes_removed"], ["fa-venv-orphan"])
        self.assertEqual(sorted(runtime.volumes), ["fastapi-architect-pip-cache", "unrelated"])
        purge = reap_leftovers(runtime, purge_cache=True)
        self.assertEqual(purge["volumes_removed"], ["fastapi-architect-pip-cache"])
        self.assertEqual(sorted(runtime.volumes), ["unrelated"])

    def test_the_probe_script_refuses_outside_a_sandbox(self) -> None:
        output = self.tmp / "probe.json"
        proc = subprocess.run([PY, str(C.PROBE_SCRIPT), "--output", str(output), "--nonce", "n1", "--expect-network", "none", "--", PY, "-c", "print('must not run')"], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, C.EXIT_PROBE_REJECTED, proc.stderr)
        self.assertNotIn("must not run", proc.stdout)
        record = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(record["violations"])
        self.assertEqual(record["nonce"], "n1")

    @unittest.skipUnless(sys.version_info >= (3, 11), "the preparation script reads pyproject.toml with tomllib")
    def test_the_preparation_script_resolves_dependencies_without_building_the_project(self) -> None:
        spec = importlib.util.spec_from_file_location("fa_prepare_env", C.PREPARE_SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        project = self.tmp / "p"
        project.mkdir()
        (project / "pyproject.toml").write_text('[project]\nname = "p"\nversion = "0"\ndependencies = ["fastapi>=0.115,<1", "pydantic>=2"]\n[project.optional-dependencies]\ntest = ["httpx"]\n[dependency-groups]\nlint = ["ruff"]\ndev = ["pytest>=8", {include-group = "lint"}]\n[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n', encoding="utf-8")
        source, req_args, specs = module.dependency_specs(str(project), ["dev", "test", "tests"])
        self.assertIn("pyproject.toml", source)
        self.assertEqual(req_args, [])
        self.assertEqual(specs, ["fastapi>=0.115,<1", "pydantic>=2", "httpx", "pytest>=8", "ruff"])
        self.assertNotIn("hatchling", specs)
        (project / "requirements.txt").write_text("requests\n", encoding="utf-8")
        source, req_args, specs = module.dependency_specs(str(project), ["dev"])
        self.assertEqual((source, specs), ("requirements files: requirements.txt", []))
        self.assertEqual(req_args, ["-r", str(project / "requirements.txt")])
        self.assertEqual(module._name_of("PyJWT[crypto]>=2.9"), "pyjwt")


# --------------------------------------------------------------------------- #
# real Docker (opt-in)
# --------------------------------------------------------------------------- #


def docker_usable() -> Tuple[bool, str]:
    if not DOCKER_WANTED:
        return False, "FA_CONTAINER=1 not set or docker not on PATH"
    try:
        info = DockerCliRuntime().info()
    except InfrastructureError as exc:
        return False, str(exc)
    return info.available, info.error or "ok"


DOCKER_OK, DOCKER_REASON = docker_usable()


@unittest.skipUnless(DOCKER_OK, f"real Docker tests are opt-in: {DOCKER_REASON}")
class DockerIsolationTests(unittest.TestCase):
    """Every test here creates containers through the executor only, from the plugin's own fixtures, and removes
    what it created. The results are evidence of the controls on this machine's daemon, nothing more."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.digest = os.environ.get("FA_CONTAINER_DIGEST")
        cls.runtime = DockerCliRuntime()
        cls.executor = ContainerExecutor(runtime=cls.runtime, image=ImagePolicy(digest=cls.digest))
        cls.executor.accepts("untrusted")
        cls.authorization = Authorization(execution=True, trust="untrusted", scope=str(FIXTURES))

    def workspace(self, source: Path, executor: Optional[ContainerExecutor] = None) -> Workspace:
        workspace = Workspace.create(str(source), (executor or self.executor).workspace_dir())
        self.addCleanup(workspace.cleanup)
        return workspace

    def assert_no_leftovers(self, nonce: str) -> None:
        self.assertEqual(self.runtime.list_labelled("container", C.LABEL_NONCE, nonce), [])
        self.assertEqual(self.runtime.list_labelled("volume", C.LABEL_NONCE, nonce), [])

    def run_sandbox(self, module: str, executor: Optional[ContainerExecutor] = None, timeout_s: float = 300.0) -> TestRun:
        executor = executor or self.executor
        executor.accepts("untrusted")
        workspace = self.workspace(SANDBOX, executor)
        command = f"python -m unittest {module}"
        try:
            return executor.run(command, workspace, ExecutionPolicy(authorized=True, command=command, timeout_s=timeout_s))
        finally:
            executor.release(workspace)

    def test_controls_hold_from_inside_and_the_evidence_is_bound_to_the_run(self) -> None:
        run = self.run_sandbox("tests.test_controls")
        env = run.environment
        self.assertEqual((run.ran, run.outcome, run.passed), (True, "passed", True), (run.stderr_tail, env.get("rejection")))
        self.assertGreaterEqual(run.counts["passed"], 9)
        self.assertEqual(run.counts.get("failed", 0), 0)
        self.assertTrue(run.verifiable)
        effective = env["effective"]
        self.assertEqual((effective["uid"], effective["gid"]), (65534, 65534))
        self.assertTrue(effective["root_readonly"])
        self.assertEqual(effective["no_new_privs"], 1)
        self.assertEqual(effective["cap_bnd"].strip("0"), "")
        self.assertEqual(effective["seccomp_mode"], 2)
        self.assertEqual(effective["network_interfaces"], ["lo"])
        self.assertNotEqual(effective["connect_attempt"], "connected")
        self.assertFalse(effective["docker_socket_present"])
        self.assertEqual(effective["home_secrets"], [])
        self.assertEqual(effective["suspicious_env_keys"], [])
        self.assertTrue(effective["tmp_noexec"])
        self.assertEqual(effective["memory_max"], str(512 * C.MIB))
        self.assertEqual(effective["pids_max"], "256")
        view = env["phases"]["run"]["daemon_view"]
        self.assertEqual((view["read_only_root"], view["network_mode"], view["privileged"]), (True, "none", False))
        self.assertEqual([c.upper() for c in view["cap_drop"]], ["ALL"])
        self.assertIn("no-new-privileges", view["security_opt"])
        self.assertEqual(sorted(m["destination"] for m in view["mounts"]), ["/evidence", "/workspace"])
        self.assertEqual(env["runtime"]["name"], "docker")
        self.assertTrue(env["runtime"]["server_version"])
        self.assertTrue(env["image"]["repo_digests"])
        self.assertEqual(env["image"]["pinned_by_digest"], bool(self.digest))
        self.assertGreaterEqual(len(env["guarantees_applied"]), 10)
        self.assertEqual(env["cleanup"]["leftovers"], [])
        self.assert_no_leftovers(env["nonce"])

    def test_a_timeout_kills_the_container_and_is_reported_as_timeout(self) -> None:
        started = time.time()
        run = self.run_sandbox("tests.test_sleep", timeout_s=15.0)
        self.assertLess(time.time() - started, 90.0)
        self.assertEqual((run.ran, run.timed_out, run.outcome, run.passed), (True, True, "timeout", False))
        self.assertEqual(run.environment["phases"]["run"]["termination"], "timeout")
        self.assert_no_leftovers(run.environment["nonce"])

    def test_the_memory_limit_ends_the_run_without_a_pass(self) -> None:
        executor = ContainerExecutor(runtime=self.runtime, sandbox=SandboxPolicy(memory_mb=128), image=ImagePolicy(digest=self.digest))
        run = self.run_sandbox("tests.test_memory", executor=executor, timeout_s=120.0)
        self.assertTrue(run.ran)
        self.assertNotEqual(run.outcome, "passed")
        phase = run.environment["phases"]["run"]
        self.assertEqual(run.environment["effective"]["memory_max"], str(128 * C.MIB))
        self.assertTrue(phase["oom_killed"] or phase["exit_code"] not in (0, None), phase)
        self.assert_no_leftovers(run.environment["nonce"])

    def test_the_pids_limit_bounds_process_creation(self) -> None:
        executor = ContainerExecutor(runtime=self.runtime, sandbox=SandboxPolicy(pids_limit=32), image=ImagePolicy(digest=self.digest))
        run = self.run_sandbox("tests.test_pids", executor=executor, timeout_s=180.0)
        self.assertEqual((run.outcome, run.passed), ("passed", True), run.stderr_tail)
        self.assertEqual(run.environment["effective"]["pids_max"], "32")
        self.assert_no_leftovers(run.environment["nonce"])

    def test_refusals_create_nothing(self) -> None:
        before = set(self.runtime.list_labelled("container", C.LABEL_OWNER, C.OWNER))
        for executor, fragment in (
            (ContainerExecutor(runtime=self.runtime, image=ImagePolicy(reference="alpine:3.20")), "allow-list"),
            (ContainerExecutor(runtime=self.runtime, sandbox=SandboxPolicy(network_run="bridge"), image=ImagePolicy(digest=self.digest)), "network_run"),
            (ContainerExecutor(runtime=self.runtime, image=ImagePolicy(reference="python:3.13-slim", digest="sha256:" + "0" * 64)), "digest"),
        ):
            with self.subTest(fragment=fragment):
                executor.accepts("untrusted")
                workspace = self.workspace(SANDBOX, executor)
                run = executor.run("python -m unittest tests.test_controls", workspace, ExecutionPolicy(authorized=True, command="x", timeout_s=30))
                self.assertEqual((run.ran, run.outcome), (False, "refused"))
                self.assertIn(fragment, run.refused or "")
                self.assertEqual(run.environment["rejection"]["kind"], "security")
        self.assertEqual(set(self.runtime.list_labelled("container", C.LABEL_OWNER, C.OWNER)), before)

    def test_an_unreachable_daemon_is_an_infrastructure_error(self) -> None:
        runtime = DockerCliRuntime(env={"DOCKER_HOST": "unix:///nonexistent/fastapi-architect.sock", "DOCKER_CONTEXT": ""}, command_timeout_s=30)
        executor = ContainerExecutor(runtime=runtime, image=ImagePolicy(digest=self.digest))
        executor.accepts("untrusted")
        workspace = self.workspace(SANDBOX, executor)
        run = executor.run("python -m unittest tests.test_controls", workspace, ExecutionPolicy(authorized=True, command="x", timeout_s=30))
        self.assertEqual((run.ran, run.outcome, run.passed), (False, "infrastructure_error", None))
        self.assertEqual(run.environment["rejection"]["kind"], "infrastructure")

    def test_an_interrupted_run_leaves_labelled_resources_that_reap_removes(self) -> None:
        identity = self.runtime.ensure_image(ImagePolicy(digest=self.digest))
        nonce = "reaptest" + str(int(time.time()))
        spec = ContainerSpec(name=f"fa-run-{nonce}", image=identity.reference, argv=["python3", "-c", "pass"], user="65534:65534", network="none", workdir="/", tmpfs={"/tmp": f"{C.TMPFS_OPTIONS},size=8m"}, labels={C.LABEL_OWNER: C.OWNER, C.LABEL_NONCE: nonce, C.LABEL_PHASE: "run"}, memory_mb=64, pids_limit=16)
        container_id = self.runtime.create(spec)
        self.runtime.volume_create(f"fa-venv-{nonce}", {C.LABEL_OWNER: C.OWNER, C.LABEL_NONCE: nonce})
        self.assertIn(container_id, self.runtime.list_labelled("container", C.LABEL_NONCE, nonce))
        report = reap_leftovers(self.runtime)
        self.assertIn(container_id, report["containers_removed"])
        self.assertIn(f"fa-venv-{nonce}", report["volumes_removed"])
        self.assertEqual(report["failed"], [])
        self.assert_no_leftovers(nonce)

    def test_design_evolution_and_remediation_verify_through_the_container(self) -> None:
        configured = ContainerExecutor(runtime=self.runtime, image=ImagePolicy(digest=self.digest))
        REGISTRY["container"] = configured
        try:
            with tempfile.TemporaryDirectory(prefix="fa-container-e2e-") as tmp:
                target = Path(tmp) / "inventory"
                authorization = Authorization(execution=True, trust="untrusted", scope=str(Path(tmp)))
                policy = ExecutionPolicy(authorized=True, command="pytest -q", timeout_s=600.0)
                report = design.run_design(str(EXAMPLES / "specs" / "inventory-service.json"), design.DesignOptions(target=str(target), authorization=authorization, policy=policy, executor="container", test_command="pytest -q"))
                tests = report["verification"]["tests"]
                self.assertTrue(tests["ran"], tests.get("denied_reason"))
                run = tests["run"]
                self.assertEqual((run["executor"], run["outcome"]), ("container", "passed"), (run.get("stderr_tail"), run["environment"].get("rejection")))
                self.assertGreaterEqual(run["counts"]["passed"], 5)  # the inventory example ships ten tests
                prepare = run["environment"]["phases"]["prepare"]
                self.assertEqual(prepare["network"], "bridge")
                self.assertFalse(prepare["pip_cache"]["shared"])
                self.assertGreater(prepare["report"]["installed_count"], 5)
                self.assertTrue(any(item.lower().startswith("fastapi==") for item in prepare["report"]["installed"]))
                self.assertEqual(run["environment"]["phases"]["run"]["spec"]["network"], "none")
                self.assertEqual(tests["workspace"]["python_files_changed_by_the_run"], [])
                self.assertEqual(report["schema"], "architecture_report/1.1")
                # evolution: the verification copy's tests run in the container (fixture profile: the pip cache is shared)
                contract = json.loads((EXAMPLES / "evolutions" / "inventory-add-suppliers.json").read_text(encoding="utf-8"))
                contract["project"]["path"] = str(target)
                contract_path = Path(tmp) / "evolve.json"
                contract_path.write_text(json.dumps(contract), encoding="utf-8")
                evolution = design.run_evolution(str(contract_path), design.EvolveOptions(verify=True, authorization=Authorization(execution=True, trust="fixture", scope=str(Path(tmp))), policy=policy, executor="container", test_command="pytest -q"))
                verification = evolution["verification"]
                self.assertEqual(verification["state"], "verified", verification["reasons"])
                self.assertEqual(verification["tests"]["run"]["executor"], "container")
                self.assertTrue(verification["tests"]["run"]["environment"]["phases"]["prepare"]["pip_cache"]["shared"])
                self.assertEqual(evolution["schema"], "evolution_report/1.4")
                # remediation: the suite of the fixture runs in the container (no dependencies to prepare)
                shop = Path(tmp) / "remediation19"
                shutil.copytree(REMEDIATION19, shop, ignore=shutil.ignore_patterns("__pycache__"))
                suite = "python -m unittest discover -s tests -t . -q"
                links = {
                    "TX_COMMIT_IN_LOOP": "python -m unittest tests.test_defects.DefectTests.test_import_is_atomic_when_an_item_fails tests.test_defects.DefectTests.test_import_commits_everything_once_on_success",
                    "TX_COMMIT_INSIDE_BEGIN_BLOCK": "python -m unittest tests.test_defects.DefectTests.test_reprice_commits_both_writes_without_error",
                    "SA_ASYNC_SESSION_NOT_AWAITED": "python -m unittest tests.test_defects.DefectTests.test_audit_trail_executes_the_statement",
                }
                remediation = run_remediation(str(shop), RunOptions(policy=ExecutionPolicy(authorized=True, command=suite, timeout_s=300.0), authorization=Authorization(execution=True, trust="untrusted", scope=str(Path(tmp))), executor="container", links=links))
                self.assertEqual(remediation["schema"], "remediation_run/1.3")
                self.assertTrue(remediation["execution"]["granted"], remediation["execution"])
                self.assertEqual(remediation["execution"]["executor"]["name"], "container")
                self.assertEqual(remediation["summary"]["by_state"], {"verified": 3}, [(c["rule"], c["state"], c.get("reason")) for c in remediation["cases"]])
                for case in remediation["cases"]:
                    self.assertEqual((case["linked_tests"]["before"]["executor"], case["linked_tests"]["after"]["executor"]), ("container", "container"))
                    self.assertEqual((case["linked_tests"]["before"]["outcome"], case["linked_tests"]["after"]["outcome"]), ("failed", "passed"))
                    self.assertTrue(case["linked_tests"]["after"]["environment"]["phases"]["prepare"]["skipped"])
                combined = remediation["combined"]
                self.assertEqual(combined["state"], "verified", combined.get("cause"))
                before = combined["suite"]["before"]
                self.assertEqual((before["executor"], before["ran"], before["outcome"]), ("container", True, "failed"), "the fixture's defects make its suite fail before the patches")
                self.assertEqual((combined["suite"]["after"]["executor"], combined["suite"]["after"]["outcome"]), ("container", "passed"))
                self.assertTrue(remediation["original_untouched"])
        finally:
            REGISTRY["container"] = ContainerExecutor()
        self.assertEqual(self.runtime.list_labelled("container", C.LABEL_OWNER, C.OWNER), [])


if __name__ == "__main__":
    unittest.main()
