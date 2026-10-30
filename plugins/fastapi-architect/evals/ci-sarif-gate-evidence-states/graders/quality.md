---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It separates gating from review by evidence state: fail on new `static_confirmed` findings of high
   severity (and optionally medium), do not fail on `insufficient` questions, which go to a review
   queue or a PR comment with their recommended test; `inferred` findings are confirmed by reading
   before they gate; `partial` means the analysis is incomplete and should be visible, not ignored.
2. It uses the mechanisms rather than a blanket policy: a committed baseline with `--baseline` and
   `--fail-on-new` so existing debt does not block, `--fail-on` for severity, SARIF upload for the
   code-scanning view, and documented in-source suppressions with a justification that the review
   can audit (never silent globs).
3. It keeps the questions valuable: `insufficient` items are turned into negative tests over time,
   and the contracts summary (unauthenticated routes that are not declared public, authorization
   unknown) is watched as a trend, not as a build failure on day one.
FAIL if it fails the build on every finding, if it drops the questions entirely, or if it proposes
suppressing by rule globally to get green.
