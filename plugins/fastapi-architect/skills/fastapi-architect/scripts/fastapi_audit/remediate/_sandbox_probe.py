#!/usr/bin/env python3
"""In-container sandbox probe (v2.1, increment 1). Standard library only; copied into the evidence mount and run
with the image's interpreter as the container's command.

It measures the restrictions **from inside** the container the authorized command is about to run in (user,
read-only root, bounding capabilities, no-new-privileges, seccomp mode, network interfaces and a connection
attempt, cgroup limits, the Docker socket, HOME and credential directories, the environment keys, the mounts and
their options), writes the record as JSON, and only then replaces itself with the command (``os.execvp``). When a
mandatory control is not in force it exits with 97 and the command never runs: the executor reports a security
rejection with the violations.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import socket
import subprocess
import sys
import tempfile

EXIT_REJECTED = 97
EXIT_NOT_FOUND = 98
SECRET_DIRS = (".ssh", ".aws", ".docker", ".kube", ".gnupg", ".netrc", ".config/gcloud", ".azure")
SECRET_ENV_PREFIXES = ("AWS_", "GITHUB_", "GH_", "DOCKER_", "SSH_", "AZURE_", "GOOGLE_", "GCLOUD_", "OPENAI_", "ANTHROPIC_", "NPM_", "PYPI_", "TWINE_")
SECRET_ENV_NAMES = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "PRIVATE_KEY")


def read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def proc_status() -> dict:
    out = {}
    for line in read_text("/proc/self/status").splitlines():
        key, _sep, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def try_write(directory: str) -> dict:
    try:
        fd, path = tempfile.mkstemp(prefix=".fa-probe-", dir=directory)
        os.close(fd)
        os.unlink(path)
        return {"writable": True, "errno": None}
    except OSError as exc:
        return {"writable": False, "errno": errno.errorcode.get(exc.errno, str(exc.errno))}


def try_exec(directory: str) -> str:
    try:
        fd, path = tempfile.mkstemp(prefix=".fa-exec-", dir=directory)
        with os.fdopen(fd, "w") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)
    except OSError as exc:
        return "error:" + errno.errorcode.get(exc.errno, str(exc.errno))
    try:
        subprocess.run([path], check=False, timeout=5)
        return "executed"
    except PermissionError:
        return "noexec"
    except OSError as exc:
        return "error:" + errno.errorcode.get(exc.errno, str(exc.errno))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def connect_attempt(host: str, port: int) -> str:
    try:
        with socket.create_connection((host, port), timeout=2):
            return "connected"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, type(exc).__name__) if getattr(exc, "errno", None) else type(exc).__name__


def mounts_of_interest(targets: tuple) -> dict:
    out = {}
    for line in read_text("/proc/self/mountinfo").splitlines():
        fields = line.split()
        if len(fields) < 6:
            continue
        mount_point, options = fields[4], fields[5]
        if mount_point in targets:
            out[mount_point] = options
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--expect-network", choices=("none", "bridge"), default="none")
    parser.add_argument("--expect-uid", type=int, default=None)
    parser.add_argument("--workspace", default="/workspace")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]

    status = proc_status()
    home = os.environ.get("HOME", "")
    interfaces = sorted(os.listdir("/sys/class/net")) if os.path.isdir("/sys/class/net") else []
    env_keys = sorted(os.environ)
    suspicious_env = sorted(k for k in env_keys if k.startswith(SECRET_ENV_PREFIXES) or any(s in k for s in SECRET_ENV_NAMES))
    record = {
        "nonce": args.nonce,
        "uid": os.getuid(),
        "gid": os.getgid(),
        "euid": os.geteuid(),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "hostname": socket.gethostname(),
        "root_readonly": not try_write("/usr/local")["writable"] and not try_write("/etc")["writable"],
        "root_write_errno": try_write("/usr/local")["errno"],
        "workspace": try_write(args.workspace),
        "tmp": try_write("/tmp"),
        "tmp_noexec": try_exec("/tmp") == "noexec",
        "no_new_privs": int(status.get("NoNewPrivs", "0") or 0),
        "cap_bnd": status.get("CapBnd", ""),
        "cap_eff": status.get("CapEff", ""),
        "seccomp_mode": int(status.get("Seccomp", "0") or 0),
        "network_interfaces": interfaces,
        "connect_attempt": connect_attempt("1.1.1.1", 80),
        "memory_max": read_text("/sys/fs/cgroup/memory.max"),
        "memory_swap_max": read_text("/sys/fs/cgroup/memory.swap.max"),
        "pids_max": read_text("/sys/fs/cgroup/pids.max"),
        "cpu_max": read_text("/sys/fs/cgroup/cpu.max"),
        "docker_socket_present": any(os.path.exists(p) for p in ("/var/run/docker.sock", "/run/docker.sock")),
        "home": home,
        "home_secrets": [name for name in SECRET_DIRS if home and os.path.exists(os.path.join(home, name))],
        "env_keys": env_keys,
        "suspicious_env_keys": suspicious_env,
        "mounts": mounts_of_interest(("/", "/workspace", "/evidence", "/venv", "/tmp", "/cache")),
        "command": command,
    }
    violations = []
    if record["uid"] == 0 or record["euid"] == 0 or record["gid"] == 0:
        violations.append("running as root")
    if args.expect_uid is not None and record["uid"] != args.expect_uid:
        violations.append(f"uid {record['uid']} differs from the expected {args.expect_uid}")
    if not record["root_readonly"]:
        violations.append("root filesystem is writable")
    if record["no_new_privs"] != 1:
        violations.append("no-new-privileges is not set")
    if record["cap_bnd"].strip("0") != "":
        violations.append(f"bounding capabilities are not empty ({record['cap_bnd']})")
    if record["seccomp_mode"] != 2:
        violations.append(f"seccomp filter is not active (mode {record['seccomp_mode']})")
    if record["docker_socket_present"]:
        violations.append("a Docker socket is reachable")
    if args.expect_network == "none":
        if [i for i in interfaces if i != "lo"]:
            violations.append(f"network interfaces present: {interfaces}")
        if record["connect_attempt"] == "connected":
            violations.append("an outbound connection succeeded")
    if record["home_secrets"]:
        violations.append(f"credential directories visible in HOME: {record['home_secrets']}")
    if suspicious_env:
        violations.append(f"secret-looking environment keys: {suspicious_env}")
    if not record["tmp"]["writable"]:
        violations.append("/tmp is not writable")
    record["violations"] = violations

    try:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=1, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        sys.stderr.write(f"sandbox probe: cannot write {args.output}: {exc}\n")
        return EXIT_REJECTED
    if violations:
        sys.stderr.write("sandbox probe: refusing to run the command: " + "; ".join(violations) + "\n")
        return EXIT_REJECTED
    if not command:
        return 0
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        sys.stderr.write(f"sandbox probe: cannot execute {command[0]!r}: {exc}\n")
        return EXIT_NOT_FOUND
    return 0  # pragma: no cover - execvp does not return


if __name__ == "__main__":
    sys.exit(main())
