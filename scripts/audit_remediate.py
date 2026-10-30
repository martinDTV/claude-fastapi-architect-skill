#!/usr/bin/env python3
"""Repository-root entry point for the verified remediation tool (v1.9).

The implementation lives inside the skill so that it ships with the plugin:
``plugins/fastapi-architect/skills/fastapi-architect/scripts/audit_remediate.py``.
This file only forwards the command line; keep logic there.

    python3 scripts/audit_remediate.py PROJECT --select TX_COMMIT_INSIDE_BEGIN_BLOCK [--authorize-execution --test-command "pytest -q"]
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

TARGET = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "fastapi-architect"
    / "skills"
    / "fastapi-architect"
    / "scripts"
    / "audit_remediate.py"
)

if __name__ == "__main__":
    if not TARGET.is_file():
        sys.stderr.write(f"audit_remediate: implementation not found at {TARGET}\n")
        sys.exit(2)
    sys.path.insert(0, str(TARGET.parent))  # the fastapi_audit package lives next to the entry
    sys.argv[0] = str(TARGET)
    runpy.run_path(str(TARGET), run_name="__main__")
