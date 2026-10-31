#!/usr/bin/env python3
"""In-container dependency preparation (v2.1, increment 1). Standard library only (``tomllib`` needs Python 3.11+,
which every allow-listed image provides); copied into the evidence mount and run inside the preparation container
as the sandbox user, with the workspace mounted read-only.

What it installs, and nothing else: the project's requirements files when present, otherwise the dependencies
declared in ``pyproject.toml`` (``[project].dependencies``, the ``dev``/``test``/``tests`` optional extras and
PEP 735 dependency groups, ``include-group`` expanded). The project itself is never built or installed: its build
backend and any hook it declares do not run; generated and conventional projects import their package through
``pythonpath``/``src`` configuration or an editable layout handled by the tests themselves. The runner named by the
authorized command (``pytest``) is added when the project does not declare it. The record written next to the
report lists the source, the specifications, pip's result and ``pip freeze`` of the prepared environment.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

REQUIREMENTS_FILES = ("requirements.txt", "requirements-dev.txt", "requirements-test.txt", "dev-requirements.txt", "test-requirements.txt", "requirements/dev.txt", "requirements/test.txt")
TAIL = 3000


def _name_of(spec: str) -> str:
    out = ""
    for ch in spec.strip():
        if ch.isalnum() or ch in "-_.":
            out += ch
        else:
            break
    return out.lower().replace("_", "-")


def _groups_from_pyproject(data: dict, wanted: list) -> list:
    specs = []
    groups = data.get("dependency-groups") or {}
    seen = set()

    def expand(name: str) -> None:
        if name in seen or name not in groups:
            return
        seen.add(name)
        for item in groups.get(name) or []:
            if isinstance(item, str):
                specs.append(item)
            elif isinstance(item, dict) and "include-group" in item:
                expand(str(item["include-group"]))

    for name in wanted:
        expand(name)
    return specs


def dependency_specs(workspace: str, wanted_groups: list) -> tuple:
    present = [name for name in REQUIREMENTS_FILES if os.path.isfile(os.path.join(workspace, name))]
    if present:
        args = []
        for name in present:
            args += ["-r", os.path.join(workspace, name)]
        return "requirements files: " + ", ".join(present), args, []
    pyproject = os.path.join(workspace, "pyproject.toml")
    if not os.path.isfile(pyproject):
        return "none", [], []
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover - images are 3.11+
        return "pyproject.toml present but tomllib unavailable in this interpreter", [], []
    with open(pyproject, "rb") as handle:
        data = tomllib.load(handle)
    project = data.get("project") or {}
    specs = [s for s in project.get("dependencies") or [] if isinstance(s, str)]
    extras = project.get("optional-dependencies") or {}
    for group in wanted_groups:
        specs += [s for s in extras.get(group) or [] if isinstance(s, str)]
    specs += _groups_from_pyproject(data, wanted_groups)
    return "pyproject.toml: [project].dependencies plus the %s extras and dependency groups" % "/".join(wanted_groups), [], specs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--venv", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--groups", default="dev,test,tests")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--ensure", action="append", default=[])
    args = parser.parse_args()
    started = time.time()
    wanted = [g.strip() for g in args.groups.split(",") if g.strip()]
    record = {"nonce": args.nonce, "python": sys.version.split()[0], "venv": args.venv, "steps": [], "installed": [], "installed_count": 0}

    def step(name: str, argv: list, timeout: int = 1800) -> int:
        t0 = time.time()
        try:
            proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", timeout=timeout)
            code, out, err = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            code, out, err = 124, (exc.stdout or ""), (exc.stderr or "") + "\ntimed out"
            if isinstance(out, bytes):
                out = out.decode(errors="replace")
            if isinstance(err, bytes):
                err = err.decode(errors="replace")
        except OSError as exc:
            code, out, err = 127, "", str(exc)
        record["steps"].append({"name": name, "argv": argv, "returncode": code, "duration_s": round(time.time() - t0, 3), "stdout_tail": (out or "")[-TAIL:], "stderr_tail": (err or "")[-TAIL:]})
        return code

    def finish(code: int) -> int:
        record["returncode"] = code
        record["duration_s"] = round(time.time() - started, 3)
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=1, sort_keys=True)
            handle.write("\n")
        return code

    source, req_args, specs = dependency_specs(args.workspace, wanted)
    record["source"] = source
    names = {_name_of(s) for s in specs}
    ensured = [name for name in args.ensure if _name_of(name) not in names and not req_args]
    record["specs"] = list(specs)
    record["ensured"] = ensured
    venv_python = os.path.join(args.venv, "bin", "python")
    if not os.path.exists(venv_python):
        if step("venv", [sys.executable, "-m", "venv", args.venv]) != 0:
            return finish(2)
    install_args = list(req_args)
    if specs or ensured:
        requirements = os.path.join(os.environ.get("TMPDIR", "/tmp"), "fa-requirements-%s.txt" % args.nonce)
        with open(requirements, "w", encoding="utf-8") as handle:
            for spec in list(specs) + list(ensured):
                handle.write(spec.strip() + "\n")
        install_args += ["-r", requirements]
    if install_args:
        pip = [venv_python, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--progress-bar", "off"]
        pip += ["--no-cache-dir"] if (args.no_cache or not args.cache_dir) else ["--cache-dir", args.cache_dir]
        if step("pip install", pip + install_args) != 0:
            return finish(3)
    else:
        record["steps"].append({"name": "pip install", "skipped": True, "reason": "nothing to install"})
    if step("pip freeze", [venv_python, "-m", "pip", "freeze", "--disable-pip-version-check"]) == 0:
        frozen = [line for line in record["steps"][-1]["stdout_tail"].splitlines() if line.strip() and not line.startswith("#")]
        record["installed"] = frozen[:300]
        record["installed_count"] = len(frozen)
    return finish(0)


if __name__ == "__main__":
    sys.exit(main())
