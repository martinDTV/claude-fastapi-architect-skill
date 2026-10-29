---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It recommends against a microservices migration now and names the real problem: missing boundaries
   inside the monolith (shared tables between billing and scheduling, no module contracts), which a network
   split would not fix and would make worse (distributed transactions, two deploy pipelines, data
   ownership).
2. It proposes the incremental path: a modular monolith with explicit module boundaries (public facades,
   enforced import contracts), each module owning its tables with cross-module reads through the facade or
   events, an outbox for cross-module effects that must survive failures, and tests that fail on boundary
   violations; deploy confidence improves from the boundaries and the tests, not from the network.
3. It gives the conditions under which extracting one service later would pay (independent scaling or
   deployment cadence proven by measurement, a different team owning it, a boundary that already holds
   in the monolith) and it keeps the tone factual toward leadership's goal.
FAIL if it endorses the quarter-long migration, if it proposes microservices as the fix for scary deploys,
or if it gives no concrete first steps.
