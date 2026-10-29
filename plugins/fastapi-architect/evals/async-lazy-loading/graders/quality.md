---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It explains that `order.lines` is a relationship with an implicit lazy load, and that under AsyncSession
   an implicit lazy load has no await point, hence MissingGreenlet (and DetachedInstanceError once the
   session is closed).
2. It gives the proper options: explicit loader options (selectinload / joinedload) on the query, or
   lazy="selectin" on the relationship, or `await session.refresh(order, ["lines"])` / `AsyncAttrs`; and
   recommends lazy="raise" (raiseload) as a guard so implicit loads fail fast in tests; it does not recommend
   switching back to sync sessions or wrapping the access in run_sync as the primary fix.
3. It notes that accessing a relationship does not by itself prove an extra query: the auditor's finding is a
   risk to confirm with a test that loads the object and touches the attribute.
FAIL if the explanation of MissingGreenlet is wrong, if it proposes sync sessions, or if it offers no
loader option.
