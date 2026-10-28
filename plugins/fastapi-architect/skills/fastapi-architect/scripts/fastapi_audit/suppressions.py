"""Documented suppressions: ``# noqa: ASYNC2xx`` and ``# audit-fastapi: ignore[RULE]``.

Only explicit markers are honoured; a bare ``# noqa`` is not. Comments are read
with the tokenizer so markers inside strings are ignored.
"""

from __future__ import annotations

import io
import re
import tokenize
from typing import Dict, Optional

# flake8-async / ruff ASYNC2xx codes that describe the same concern as ASYNC_BLOCKING_CALL.
NOQA_CODES_BY_RULE: Dict[str, frozenset] = {
    "ASYNC_BLOCKING_CALL": frozenset(
        {
            "ASYNC210",  # blocking HTTP call
            "ASYNC220",  # subprocess / process in async
            "ASYNC221",
            "ASYNC222",
            "ASYNC230",  # blocking file open
            "ASYNC231",
            "ASYNC232",
            "ASYNC240",  # blocking os.path call
            "ASYNC250",  # input()
            "ASYNC251",  # time.sleep
        }
    ),
    # SQLAlchemy sync I/O in async code is the same concern flake8-async names ASYNC2xx
    "SA_SYNC_SESSION_IN_ASYNC_DEF": frozenset({"ASYNC210", "ASYNC220"}),
}
NOQA_RE = re.compile(r"#\s*noqa\s*:\s*([A-Za-z0-9_,\s]+)")
# Tool-specific marker: ``# audit-fastapi: ignore`` or ``# audit-fastapi: ignore[RULE_A, RULE_B]``
MARKER_RE = re.compile(r"#\s*audit-fastapi:\s*ignore(?:\[([A-Za-z0-9_,\s]+)\])?")


def line_comments(source: bytes) -> Dict[int, str]:
    """Map line number -> comment text, using the tokenizer (never regexes over strings)."""
    comments: Dict[int, str] = {}
    try:
        for token in tokenize.tokenize(io.BytesIO(source).readline):
            if token.type == tokenize.COMMENT:
                comments[token.start[0]] = token.string
    except (tokenize.TokenError, SyntaxError, UnicodeDecodeError, ValueError):
        pass  # no suppressions can be read; findings stay active
    return comments


def suppression_for(rule: str, comment: Optional[str]) -> Optional[str]:
    """Return the documented suppression that applies to ``rule`` on this line, if any."""
    if not comment:
        return None
    marker = MARKER_RE.search(comment)
    if marker:
        rules = marker.group(1)
        if rules is None:
            return "audit-fastapi: ignore"
        wanted = {r.strip() for r in rules.split(",") if r.strip()}
        if rule in wanted:
            return f"audit-fastapi: ignore[{rule}]"
    noqa = NOQA_RE.search(comment)
    if noqa:
        codes = {c.strip().upper() for c in noqa.group(1).split(",") if c.strip()}
        matched = sorted(codes & NOQA_CODES_BY_RULE.get(rule, frozenset()))
        if matched:
            return "noqa: " + ", ".join(matched)
    return None
