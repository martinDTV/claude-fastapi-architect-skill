# truth16: ground-truth corpus for the v1.6 engine

Every expectation lives next to the code as a trailing marker, so line numbers cannot drift:

- `# truth: RULE_ID` — the auditor must report RULE_ID on this line.
- `# truth-absent: RULE_ID` — the auditor must not report RULE_ID on this line (negative).
- `# truth-edge: dotted.callee` — this call site must resolve to that project function.
- `# truth-unresolved: STATUS` — this call site must stay unresolved with that status
  (`ambiguous`, `dynamic`, `external`, `builtin`, `object`).

Markers were written before the v1.6 detectors ran (see `scripts/audit_precision.py`), and the
corpus is scored for both the previous and the current engine. Nothing here is executed.
