#!/usr/bin/env python3
"""Repository-root entry point for the FastAPI static auditor.

The implementation lives inside the skill so that it ships with the plugin:
``plugins/fastapi-architect/skills/fastapi-architect/scripts/audit_fastapi.py``.
This file only forwards the command line to it; keep logic there.

    python3 scripts/audit_fastapi.py /path/to/project [--json] [--output report.json]
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
    / "audit_fastapi.py"
)

if __name__ == "__main__":
    if not TARGET.is_file():
        sys.stderr.write(f"audit_fastapi: implementation not found at {TARGET}\n")
        sys.exit(2)
    sys.argv[0] = str(TARGET)
    runpy.run_path(str(TARGET), run_name="__main__")
