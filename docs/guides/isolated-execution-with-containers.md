# Isolated execution with containers

v2.1 adds a second executor to the trust model: `container`. It runs the tests of a generated project, of an
evolution's verification copy or of a remediation copy inside an OCI container with restrictions that are verified
twice per run, and it is the only executor that accepts the `untrusted` profile. The local executor (`local`) stays
for the `fixture` and `trusted` profiles with its documented limits: process hygiene, not a sandbox. There is no
fallback from one to the other: when Docker is missing or refuses, an untrusted project stays unverified.

This guide covers how to use it, what it enforces and how that is verified, what it does not protect against, and
how the dependency preparation works. The contract is in `references/safe-refactoring.md` (trust model and
executors); the decision record is `docs/architecture/adr-2.1-03-isolated-container-executor.md`.

## Using it

```
python3 scripts/design_fastapi.py SPEC.json --target out/ \
    --authorize-execution --trust untrusted --trust-scope out/ --executor container --test-command "pytest -q"

python3 scripts/evolve_fastapi.py plan EVOLUTION.json \
    --authorize-execution --trust untrusted --trust-scope PROJECT --executor container --test-command "pytest -q"

python3 scripts/audit_remediate.py PROJECT --authorize-execution --trust untrusted --trust-scope PROJECT \
    --executor container --test-command "python -m unittest discover -s tests -t . -q" --format json

python3 scripts/audit_remediate.py . --container-reap          # remove containers and volumes an interrupted run left
```

Options, identical on the three tools (`--container-*`): `--container-image` (allow-listed: `python:3.13-slim`,
`python:3.12-slim`, `python:3.11-slim`; anything else needs `--container-digest`), `--container-digest sha256:...`
(the image must carry it; recorded as pinned), `--container-no-pull`, `--container-user UID:GID|host` (never root),
`--container-memory-mb` (512), `--container-cpus` (1.0), `--container-pids` (256), `--container-tmp-mb` (256),
`--container-prepare-network bridge|none` (the test run never has a network), `--container-prepare-timeout`,
`--container-workspace-base DIR`, `--container-no-pip-cache`, `--container-runtime docker`. Every option can tighten
or pin; none can weaken a mandatory restriction, and a policy that tries is refused before anything runs.

The command must be `python ...`, `python -m pytest ...`, `python -m unittest ...` or `pytest ...`: those are the
programs the image provides (`uv` is not in the image). The runner adapters are the same as for the local executor,
so the outcome comes from the nonce-bound JUnit or JSON report, never from the exit code.

## What a run does

1. **Policy check.** The sandbox and image policies are validated. A weakened mandatory restriction, an image outside
   the allow-list without a digest, a `uv` command: refused, nothing created.
2. **Runtime identity.** `docker version` and `docker info` are recorded: server version, operating system, kernel,
   cgroup version, security options, rootless or not. A daemon that does not answer is an infrastructure error.
3. **Image identity.** The image is inspected (pulled only when missing and pulling is allowed); its repository
   digests and id are recorded; a configured digest must be among them or the run is refused. A pinned digest that
   does not exist for the repository is a security rejection, not an infrastructure error.
4. **Mount visibility.** A read-only probe container checks that the daemon can read the workspace. With Colima
   the daemon runs in a VM that shares `$HOME` and `/tmp/colima`; with Docker Desktop, `/Users`, `/Volumes`,
   `/private` and `/tmp`. The workspace copies therefore live under `~/.cache/fastapi-architect/workspaces` by
   default (`--container-workspace-base` to change it); a temporary directory under `/var/folders` would be
   invisible to Colima.
5. **Preparation** (only when the project declares dependencies): a volume is created and handed to the sandbox
   user by the image's `chown` (root with `CAP_CHOWN` only, nothing from the project mounted, no network); then a
   non-root container with the workspace mounted **read-only**, the volume read-write, a size-limited `noexec`
   tmpfs and the network the policy allows (`bridge` by default, `none` when everything is already in the image)
   builds a virtual environment from the project's requirements files or from `pyproject.toml` (`[project].dependencies`
   plus the `dev`/`test`/`tests` extras and dependency groups). The project itself is never built or installed: no
   build backend, no hook. `pytest` is added when the command names it and the project does not declare it. The pip
   cache volume is shared only for the `fixture` and `trusted` profiles: an untrusted project could poison it for
   later runs. The record keeps `pip freeze` of the result.
6. **Run.** A second container: network `none`, user `65534:65534` (or the configured non-root ids), read-only root
   filesystem, every capability dropped, `no-new-privileges`, the runtime's built-in seccomp profile, memory with no
   swap, CPU quota, pids limit, `nofile`/`fsize`/`cpu` ulimits, `/tmp` as a `rw,noexec,nosuid,nodev` tmpfs, and exactly
   these mounts: the workspace copy (rw), the evidence directory (rw) and the prepared environment (ro). The
   environment holds only the keys the executor sets (`PATH` with the prepared environment first, `HOME=/tmp`, the
   Python hygiene variables, the locale).
7. **Verification before start.** `docker inspect` of the created container is compared with the specification
   (`verify_effective`): user, read-only root, capabilities, security options, network mode, memory and swap, pids,
   CPU, devices, namespaces, tmpfs options, and every mount (type, source, destination, mode; no source from the
   Docker socket, the system directories, the home directory itself or its credential directories). Any difference
   is a security rejection: the container is removed without starting.
8. **Verification from inside.** The container's command is `_sandbox_probe.py`, which records uid and gid,
   whether the root filesystem accepts writes, the bounding capabilities, `NoNewPrivs`, the seccomp mode, the network
   interfaces and the result of a connection attempt, the cgroup limits, the presence of a Docker socket, `HOME` and
   its credential directories, the environment keys and the mounts; writes the record into the evidence directory;
   refuses (exit 97) when a mandatory control is absent; and only then `exec`s the authorized command.
9. **Outcome.** The runner's report is read from the host side of the evidence mount and assessed with the same
   rules as the local executor. `exit 0` without a matching report is `no_evidence`, never `passed`. A timeout kills
   the container (`timeout`); an OOM kill is recorded (`oom_killed`) and the run is not a pass.
10. **Cleanup.** Containers are removed; the prepared volume is removed when the consumer releases the workspace;
    everything carries the labels `fastapi-architect.owner`, `fastapi-architect.nonce`, `fastapi-architect.phase`, so
    `--container-reap` removes what an interruption left.

## What the report records

`verification.tests.run.environment` (design and evolution reports, schemas `architecture_report/1.1` and
`evolution_report/1.2`) and every `TestRun` of a remediation run (`remediation_run/1.3`) carry: executor, isolation
level, trust profile, nonce, runtime (name, versions, operating system, kernel, cgroup version, security options,
rootless, trust boundary), image (reference, id, repository digests, pinned or not, provenance note), the sandbox
policy, the phases (volume init with its user and capability, preparation with its network, mounts, pip cache
decision, report and timing; run with its specification, the daemon's view, exit code, OOM flag, termination reason
and timing), the in-container record (`effective`), the guarantees that were verified for this run, the limitations,
the cleanup (containers removed, volumes kept or removed, leftovers) and any rejection (security or infrastructure).
All of it is additive: readers of the previous schemas keep working.

## What it does not protect against

Written in every run under `limitations`, and worth repeating here:

- **The runtime and the kernel.** The kernel is shared. A vulnerability in `runc`, `containerd`, the daemon or the
  kernel is outside what this executor can defend against. Nothing here claims absolute isolation.
- **The daemon.** A rootful daemon is root-equivalent on its host. Whoever controls the socket controls the sandbox.
  On this project's development machine the daemon runs rootful inside a Colima VM (Ubuntu, cgroup v2, seccomp
  builtin, AppArmor); a breakout would reach that VM, and the macOS host only through the shared directories. A
  rootless daemon narrows the boundary to the daemon's user; the report says which one answered.
- **Preparation egress.** The preparation container has network access when the policy allows it; it is not
  restricted to a package index. Set `--container-prepare-network none` when the image already has everything or
  the project needs nothing, and prefer lock files.
- **Image provenance.** Only the repository digest is recorded and, when configured, enforced. Signatures are not
  verified. Pin a digest to make the image identity a precondition.
- **No user namespace remapping.** uid 65534 inside is uid 65534 on the daemon host.
- **Bind mounts are exec-capable.** Docker cannot mount a bind mount `noexec`; the workspace copy can therefore hold
  executable files. `/tmp` and the prepared environment do not (tmpfs `noexec`, volume read-only).
- **Side channels, resource exhaustion of the daemon host beyond the cgroup limits, and anything the tests can do
  to the workspace copy itself** (it is a copy; the project is never mounted).

## Verified on this project's machine

`FA_CONTAINER=1 python3 -m unittest discover -s tests -p test_container_v21.py` runs the opt-in suite. The
`container21/sandbox` fixture asserts the controls from inside (not root; read-only root; no Docker socket; HOME
without credentials and no secret-looking environment keys; `NoNewPrivs`, empty capability sets, seccomp mode 2;
no interface besides `lo` and a failed connection; cgroup memory and pids limits; `/tmp` writable and `noexec`;
only the expected mounts; privilege-escalation binaries do not change the id). Separate tests prove the timeout
kills the container, a 128 MiB limit ends a 1 GiB allocation without a pass, a pids limit of 32 bounds process
creation, refusals create nothing, an unreachable daemon is an infrastructure error, labelled leftovers are reaped,
and the three consumers verify through the container (an untrusted generated project with its dependencies
prepared, an evolution under the fixture profile with the shared pip cache, a remediation suite with nothing to
prepare). The numbers of the last run are in `docs/releases/RELEASE-NOTES-v2.1.md`.
