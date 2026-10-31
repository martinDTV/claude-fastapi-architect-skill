# ADR 2.1-03 — Isolated execution in OCI containers, Docker first

Status: accepted (2026-10-31, decisions of the v2.1.0 review) and implemented in increment 1. Supersedes the
"Towards v2.0" note of `references/safe-refactoring.md` that promised an executor with `isolation_level: isolated`.

## Context

Since v1.9 the plugin executes project code only through the trust model: an explicit `Authorization` (execution,
trust profile, scope) and an `Executor` that accepts the profile. The only executor was `LocalProcessExecutor`:
process hygiene (copy of the project, fresh HOME, reduced environment, no shell, allow-listed programs, timeout,
CPU and file-size limits), documented as **not** a sandbox. Consequently the `untrusted` profile, the default, could
never execute anything: the design, evolution and remediation flows ended `audit_clean` or `inconclusive` for any
project the operator did not vouch for. The v2.0.0 guarantees repeated this as `not_claimed` on every report.

The v2.1.0 discovery evaluated four options (OCI container through the runtime CLI; macOS `sandbox-exec`;
Linux user namespaces with `bwrap`; microVMs). The review decided: OCI containers, Docker as the first runtime, an
interface that admits Podman later without coupling, and a list of mandatory adjustments (dependency preparation in a
restricted environment, explicit policies for filesystem, network, user, capabilities, memory, CPU and processes, no
Docker socket or host credentials, images by digest where viable, fail closed without a suitable executor, no claim of
absolute isolation).

## Decision

1. **One seam, two executors.** `ContainerExecutor` implements the existing `Executor` contract next to
   `LocalProcessExecutor`; both are selected by `select_executor` from the same `Authorization`. Two hooks were added
   to the contract, with no-op defaults: `workspace_dir()` (an executor whose runtime runs in a VM needs the copy under
   a shared path) and `release(workspace)` (drop what was prepared for a workspace). The three consumers call them at
   the places where they already created and destroyed workspaces. There is no other integration.
2. **No fallback.** `untrusted` is accepted only by the container executor. When the runtime is missing, the daemon
   does not answer, the image cannot be resolved or the mounts are not visible, the run is an infrastructure error
   and the consumer's state is `inconclusive`; when a policy, an image or an effective configuration would weaken a
   mandatory restriction, the run is a security rejection (`refused`). Neither path touches the local executor.
3. **Runtime-neutral domain.** `SandboxPolicy`, `ImagePolicy`, `ContainerSpec`, `Mount`, `ContainerState`,
   `ImageIdentity`, `RuntimeInfo` and `verify_effective` know nothing about Docker. `ContainerRuntime` is the
   adapter interface (`info`, `ensure_image`, `create`, `inspect`, `start_and_wait`, `state`, `remove`, volumes,
   `list_labelled`); `DockerCliRuntime` is the only implementation, driving the `docker` CLI with argv lists (no
   shell, a minimal client environment). A Podman adapter is the same interface with its own argv translation.
4. **Mandatory restrictions, verified twice.** Non-root user, read-only root, every capability dropped,
   `no-new-privileges`, the runtime's seccomp profile, `network: none` for the run, memory without swap, CPU, pids,
   ulimits, a `noexec` tmpfs for `/tmp`, exactly three mounts. The daemon-reported configuration of the created
   container is compared with the specification **before start**; the container's entrypoint is a probe that measures
   the same controls **from inside** and refuses to exec the command when one is absent. Both records are in the run.
5. **Preparation in a container, never on the host.** Dependencies are installed into a volume by a non-root
   container with the workspace read-only and the network the policy allows (`bridge` by default, `none` on
   demand); the project is never built (no build backend, no hooks); only requirements files or the declared
   dependencies and dev/test groups are installed; the pip cache volume is shared only for `fixture`/`trusted`
   profiles. The volume is handed to the sandbox user by the image's `chown` running as root with `CAP_CHOWN` only and
   nothing from the project mounted; this is recorded as a phase of its own.
6. **Evidence first.** The same runner adapters produce the nonce-bound report (paths translated to the evidence
   mount); the outcome is assessed from the report. `TestRun` gains an additive `environment` record (runtime,
   image, policy, phases, effective controls, guarantees applied, limitations, cleanup, rejection). The report schemas
   bump additively: `remediation_run/1.3`, `architecture_report/1.1`, `evolution_report/1.2`.
7. **Honest limits.** Every run lists what is not provided: runtime and kernel vulnerabilities, the daemon's trust
   boundary (rootful is root-equivalent on its host), preparation egress, image provenance beyond the digest, no
   user namespace remapping, exec-capable bind mounts. The guarantees fixtures of the design engine were reworded to
   name the executor each claim depends on.

## Consequences

- Untrusted projects can be verified for the first time, with evidence of what was in force. Operators without a
  usable daemon keep exactly the v2.0 behaviour.
- The development machine's daemon (Colima, rootful inside a VM) is a weaker boundary than a rootless daemon on
  Linux; the report names the boundary and the guide explains it. The tests record results for this daemon only.
- Preparation costs time (a cold `pip install` of a generated project is tens of seconds) and needs a network; the
  shared cache helps trusted and fixture runs only, by design.
- Two decisions deferred: the PostgreSQL sidecar for database-backed suites inside the isolated executor (a
  separate increment) and a `confined` level based on `sandbox-exec`/`bwrap` for machines without a container
  runtime.
