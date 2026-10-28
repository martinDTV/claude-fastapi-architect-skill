---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It advises against the reorganisation: the existing hexagonal layout with enforced import-linter contracts already provides the properties the modular-monolith layout aims for (clear dependency direction, testable domain, swappable adapters), and the guide's default layout is a default for teams without a coherent structure, not a mandate.
2. It explains what would justify changing (concrete pain: contracts not enforced, domain leaking infrastructure, modules impossible to extract, team confusion) and proposes incremental options if pain exists (e.g. grouping by capability inside the existing layers) rather than a rewrite.
3. It recommends keeping import-linter as the source of truth for boundaries and running it in CI, and it does not invent problems with the current structure.
FAIL if it recommends the rewrite, if it claims one layout is universally correct, or if it ignores the existing contracts.
