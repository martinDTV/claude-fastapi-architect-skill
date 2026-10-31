"""Shared command-line surface of the container executor (v2.1, increment 1): the same flags on
``design_fastapi.py``, ``evolve_fastapi.py`` and ``audit_remediate.py``, one configured executor registered under
the name ``container``. Nothing here runs a container; the executor does, and only through the trust model."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from .containers import ALLOWED_IMAGES, DEFAULT_IMAGE, NETWORK_BRIDGE, NETWORK_NONE, ContainerExecutor, DockerCliRuntime, ImagePolicy, InfrastructureError, SandboxPolicy, reap_leftovers
from .executors import REGISTRY


def add_container_arguments(parser: argparse.ArgumentParser, *, reap: bool = False) -> None:
    group = parser.add_argument_group("container executor (--executor container)", "isolated execution in an OCI container; every option can only tighten or pin, never weaken the mandatory restrictions (non-root, read-only root, no capabilities, no-new-privileges, seccomp, no network for the run)")
    group.add_argument("--container-runtime", choices=("docker",), default="docker", help="runtime adapter (docker; the interface admits others)")
    group.add_argument("--container-image", default=DEFAULT_IMAGE, metavar="REF", help=f"image reference; allow-listed without a digest: {', '.join(ALLOWED_IMAGES)}; anything else needs --container-digest")
    group.add_argument("--container-digest", metavar="SHA256", help="sha256:... the image must carry (recorded as pinned; refused when it differs)")
    group.add_argument("--container-no-pull", action="store_true", help="fail closed when the image is not present instead of pulling it")
    group.add_argument("--container-user", default="65534:65534", metavar="UID:GID|host", help="non-root user inside the container (default nobody); 'host' uses the invoking user's ids")
    group.add_argument("--container-memory-mb", type=int, default=512, metavar="MB")
    group.add_argument("--container-cpus", type=float, default=1.0, metavar="N")
    group.add_argument("--container-pids", type=int, default=256, metavar="N")
    group.add_argument("--container-tmp-mb", type=int, default=256, metavar="MB", help="size of the noexec tmpfs at /tmp")
    group.add_argument("--container-prepare-network", choices=(NETWORK_BRIDGE, NETWORK_NONE), default=NETWORK_BRIDGE, help="network of the dependency-preparation container only; the test run never has one")
    group.add_argument("--container-prepare-timeout", type=float, default=900.0, metavar="SECONDS")
    group.add_argument("--container-workspace-base", metavar="DIR", help="directory the daemon can see for the workspace copies (default ~/.cache/fastapi-architect/workspaces)")
    group.add_argument("--container-no-pip-cache", action="store_true", help="never share the pip cache volume (it is already not shared for untrusted projects)")
    if reap:
        group.add_argument("--container-reap", action="store_true", help="remove every container and prepared volume the executor left behind, then exit")


def container_executor_from_args(args: argparse.Namespace) -> ContainerExecutor:
    sandbox = SandboxPolicy(
        user=args.container_user,
        memory_mb=args.container_memory_mb,
        cpus=args.container_cpus,
        pids_limit=args.container_pids,
        tmp_size_mb=args.container_tmp_mb,
        network_prepare=args.container_prepare_network,
        prepare_timeout_s=max(1.0, args.container_prepare_timeout),
        workspace_base=args.container_workspace_base,
        pip_cache_volume=None if args.container_no_pip_cache else "fastapi-architect-pip-cache",
    )
    image = ImagePolicy(reference=args.container_image, digest=args.container_digest, pull=not args.container_no_pull)
    executor = ContainerExecutor(runtime=DockerCliRuntime(), sandbox=sandbox, image=image)
    REGISTRY[ContainerExecutor.name] = executor
    return executor


def maybe_reap(args: argparse.Namespace, prog: str) -> Optional[int]:
    """``--container-reap``: remove leftovers and return an exit code; None when the flag was not given."""
    if not getattr(args, "container_reap", False):
        return None
    try:
        report = reap_leftovers(DockerCliRuntime())
    except InfrastructureError as exc:
        sys.stderr.write(f"{prog}: container runtime not usable: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0 if not report["failed"] else 1
