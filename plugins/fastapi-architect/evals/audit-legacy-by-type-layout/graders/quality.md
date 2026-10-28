---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It does not propose a big-bang rewrite or a one-shot mass move of all files; it proposes an incremental migration (one module or seam at a time) toward a modular monolith organised by business capability, and it explicitly rejects microservices for a single team of four.
2. The first steps include establishing a safety net before moving code: characterization tests and/or an OpenAPI contract snapshot for the paths being touched, and it recommends enforcing boundaries mechanically (import-linter contracts or equivalent) once a boundary exists.
3. It distinguishes structural/style gaps (folder layout, naming) from functional defects or risks, and it does not claim the current code is broken merely because of the layout.
FAIL if it recommends microservices, a single large reorganisation PR, or starting the refactor without any tests.
