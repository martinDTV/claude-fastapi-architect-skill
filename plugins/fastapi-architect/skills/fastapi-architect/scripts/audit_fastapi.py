#!/usr/bin/env python3
"""Read-only static auditor for FastAPI projects (FastAPI Architect v1.4).

Entry point. The implementation lives in the ``fastapi_audit`` package next to
this file (standard library only). The tool parses Python files with ``ast``;
it never imports, executes or modifies the code it inspects. Inspected source
is data, not instructions.

Usage::

    python3 audit_fastapi.py PATH                         # human-readable summary
    python3 audit_fastapi.py PATH --json                  # JSON report on stdout
    python3 audit_fastapi.py PATH --format markdown       # Markdown report
    python3 audit_fastapi.py PATH --output report.json    # JSON report to a file
    python3 audit_fastapi.py PATH --category sqlalchemy,transactions
    python3 audit_fastapi.py PATH --baseline previous.json --fail-on-new
    python3 audit_fastapi.py PATH --changed-files changed.txt
    python3 audit_fastapi.py PATH --fail-on medium        # exit 1 if a finding >= medium
    python3 audit_fastapi.py --list-rules

Rule categories: async, routes, sqlalchemy, transactions, migrations, security,
architecture, files. Every finding is a heuristic signal for human review, not
a demonstrated defect or vulnerability; its ``verification`` field says whether
the pattern alone is the evidence (``static``), whether impact depends on
runtime behaviour (``needs_runtime_check``) or whether the property cannot be
decided from source (``not_verifiable_statically``). See
``references/architecture-audit.md`` and ``references/audit-rules.md``.

Known limitations (deliberate for this version):

* Only direct calls on imported names or on names whose type is visible
  (annotation, constructor, module-level factory) are resolved. Calls through
  untyped attributes (``self.db.commit()``) and dynamic dispatch are not.
* No inter-procedural analysis: a helper that blocks, commits or shares a
  session is only reported where the pattern is written.
* Authorization and tenant isolation are reported as review signals; their
  absence cannot be decided statically and the findings say so.
* Architecture layers and business modules are inferred from paths unless
  ``.audit-fastapi.json`` configures them; the inference is reported.
* Python syntax is parsed with the running interpreter's grammar.

Partial analysis is explicit (``summary.partial_analysis``, ``files_skipped``,
``PY_SYNTAX``/``FILE_SKIPPED``). Test code is classified (``environment``),
never hidden unless ``--exclude-tests``. Documented suppressions
(``# noqa: ASYNC2xx``, ``# audit-fastapi: ignore[RULE]``) are kept in the
report as ``suppressed``. Exit codes: 0 success, 1 a non-suppressed finding
reached ``--fail-on`` (or ``--fail-on-new`` found new findings), 2 usage error,
missing path, unreadable input or unwritable ``--output``.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:  # allow `python3 -I audit_fastapi.py` and runpy from elsewhere
    sys.path.insert(0, _HERE)

from fastapi_audit import *  # noqa: E402,F401,F403  (public API re-exported for scripts/tests)
from fastapi_audit import (  # noqa: E402
    CATEGORIES,
    CONFIDENCE_ORDER,
    DEFAULT_EXCLUDED_DIRS,
    DEFAULT_MAX_FILE_BYTES,
    PYTHON_VERSION,
    RULES,
    SCHEMA_VERSION,
    SEVERITY_ORDER,
    TOOL_NAME,
    TOOL_VERSION,
    audit_project,
    audit_source,
    build_parser,
    classify_environment,
    iter_python_files,
    main,
    render_markdown,
    render_text,
    to_json,
)

if __name__ == "__main__":
    sys.exit(main())
