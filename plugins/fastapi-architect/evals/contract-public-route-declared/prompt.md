---
name: contract-public-route-declared
tags: [v1.7, positive, decision]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

The auditor's `contracts` block lists `POST /webhooks/stripe` and `GET /pricing` as "authentication: absent,
public: false" and reports `SEC_ROUTE_WITHOUT_AUTH_SIGNAL` on both. They are meant to be public: the webhook
verifies Stripe's signature inside the handler and pricing is a marketing page. How do we make the
contract reflect that without weakening the audit for everything else?
