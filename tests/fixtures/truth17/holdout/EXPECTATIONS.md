# truth17 / holdout — third partition, written on 2026-10-30 before the remaining v1.7 work

Written after the `dev` and `acceptance` results were known but before implementing the evidence states,
the SARIF export, the declarative architectural contracts and the consumer/inbox signal. It is scored
exactly once, with the final engine, and is never edited. It differs from the other partitions in domain
(logistics: depots as tenants, declared with `tenant_params`), in shapes (an optional-authentication
dependency, a resource loader dependency, a scope class, router-level membership inherited by routes, a
tracker object holding session and client, declared architectural contracts in `.audit-fastapi.json`) and
in declaring its tenant parameter up front, as a real project would.

Declared contracts (opt-in): every `/api/*` route must be authenticated; mutations under `/api/*` must
show an authorization kind; `logistics.api` must not import `logistics.infra`; commits belong to the
service layer. Rules expected for them: `CONTRACT_ROUTE_AUTH_REQUIRED`, `CONTRACT_MUTATION_AUTHZ_REQUIRED`,
`CONTRACT_IMPORT_FORBIDDEN`, `CONTRACT_COMMIT_OUTSIDE_LAYER`. Consumer without a redelivery guard:
`REL_CONSUMER_WITHOUT_INBOX` as a question. Test code keeps its shared-session finding (policy).

## Change log
- (none)
