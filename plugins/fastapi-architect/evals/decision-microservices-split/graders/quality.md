---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It does not deliver a microservice split. It explains that the hard criteria for extraction (independent scaling needs, separate teams blocked by each other's releases, team ownership of a service, isolation or compliance boundaries, different runtime needs) are not met by one team of five with one database and one deployable, and it names which criteria are missing.
2. It proposes the alternative that captures most of the benefit: enforcing module boundaries inside the monolith (modules organised by business capability, public facades between modules, mechanical enforcement such as import-linter contracts), with concrete first steps.
3. It states what would change the decision later (which criteria, if they became true, would justify extracting a specific module) and warns about the distributed-monolith failure mode and data ownership.
FAIL if it produces a service-by-service split plan, rejects the request without reasoning, or offers no concrete alternative.
