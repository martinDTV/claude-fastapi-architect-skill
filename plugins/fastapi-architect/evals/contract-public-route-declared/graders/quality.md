---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It explains that an intentionally public route must be declared, not silently accepted: the project
   declares it with `public_routes` globs in `.audit-fastapi.json` or an `# audit-fastapi: public` marker on
   the route, and the contract then shows `public: true` with its source, so the decision is reviewable.
2. It distinguishes the two cases: the webhook is not really "public" in the security sense, it is
   authenticated by signature verification inside the handler; it recommends keeping that verification
   (constant-time comparison, replay protection via timestamp/idempotency) and a test that rejects a bad
   signature, while declaring the route public for the auditor; the pricing page is public by design.
3. It warns against blanket globs (`/*`) or disabling the rule globally, and suggests reviewing the
   `contracts` summary (`unauthenticated_not_public`) in CI so new routes cannot become public by accident.
FAIL if it suggests ignoring the rule for the whole project, if it says the webhook needs no verification,
or if it does not name a concrete declaration mechanism.
