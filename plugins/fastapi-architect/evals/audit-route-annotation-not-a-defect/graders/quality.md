---
type: llm
weight: 2
focus: last_message
---

Evaluate only the final response. PASS requires ALL of:
1. It states clearly that the finding is informational, not a bug and not a vulnerability by itself, and that no security incident is warranted on this evidence.
2. It explains that a return annotation or response_model gives FastAPI a response schema for validation and field filtering, and that routes with response_model already have that contract (so a report flagging them is a false positive or the tool should suppress them).
3. It prioritises by risk rather than blanket changes: routes that return ORM objects, dicts with sensitive fields, or data that must be filtered come first; the rest can be handled gradually.
FAIL if it calls the API broken, recommends a security incident, or recommends urgently changing all 40 routes without prioritisation.
