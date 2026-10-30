---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It picks contracts that match the stated pain and names them concretely: `commit_layers` so only
   services own transactions, `forbidden_imports` from `api` to `infra`, `route_auth_required` for the
   API prefix, and `mutation_authorization_required` for mutating routes; it explains each as "the code
   disagrees with what we wrote down", not as a style opinion.
2. It gives a rollout that does not block: declare, run, commit a baseline, gate only on new violations
   (`--fail-on-new`), fix existing ones per module with owners, and tighten later; it may mention
   import-linter as the source of truth for boundaries when it already exists.
3. It explains the JSON point: configuration that is data cannot execute code, so the auditor can run on
   untrusted branches and in CI without importing the project, and the declarations stay reviewable in
   diffs; a Python config would require executing the repository's code to know its rules.
FAIL if it recommends turning on everything as a hard failure immediately, if it proposes microservices as
the answer to the boundary problem, or if it says the configuration format does not matter.
