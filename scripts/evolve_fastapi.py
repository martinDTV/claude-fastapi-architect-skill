#!/usr/bin/env python3
"""Repository-root entry point for the evolution engine (v2.0, increment 3).

The implementation lives inside the skill so that it ships with the plugin:
``plugins/fastapi-architect/skills/fastapi-architect/scripts/evolve_fastapi.py``.
This file only forwards the command line; keep logic there.

    python3 scripts/evolve_fastapi.py discover PROJECT
    python3 scripts/evolve_fastapi.py plan CONTRACT.json
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
    / "evolve_fastapi.py"
)

if __name__ == "__main__":
    if not TARGET.is_file():
        sys.stderr.write(f"evolve_fastapi: implementation not found at {TARGET}\n")
        sys.exit(2)
    sys.path.insert(0, str(TARGET.parent))  # the fastapi_audit and fastapi_design packages live next to the entry
    sys.argv[0] = str(TARGET)
    runpy.run_path(str(TARGET), run_name="__main__")
