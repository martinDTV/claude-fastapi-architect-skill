# Domain modeling, DDD and architecture trade-offs

## Trigger
Complex invariants, multiple bounded contexts, unclear ownership, domain events, disputes about Clean Architecture, CQRS or event sourcing.

## Discover the domain before selecting a pattern
1. Write the five most important use cases as commands, actor, preconditions, invariant, state transition and observable result.
2. Build a glossary with product owners; detect overloaded terms (e.g. customer, account, owner) and conflicting lifecycles.
3. Identify consistency boundaries from invariants, not folder names or HTTP routes. Assign an aggregate root only when transactional consistency requires one.
4. Map inbound and outbound dependencies; identify system of record and who may mutate each entity.
5. Classify integration as same-transaction, synchronous query, asynchronous notification or eventual-consistency workflow.

## Pattern selection
| Situation | Prefer | Avoid unless demonstrated |
|---|---|---|
| CRUD / simple rules | Feature modules; straightforward service/repository | Domain entity factories, abstract interfaces for every class |
| Complex local invariants | Pure domain functions or entities plus application service | ORM callbacks as principal business policy |
| Multiple adapters/providers | Hexagonal ports at the uncertain seam | Dependency inversion across every function |
| Many dissimilar read shapes | Dedicated read query models; perhaps CQRS-lite | Separate read infrastructure by default |
| Auditable immutable history | Append-only history + transactional records | Full event sourcing without replay projections and migration plan |
| Independent domain ownership | Bounded context with explicit API and events | Distributed service solely because of folder size |

## Application boundaries
- Transport adapters validate transport shapes and map HTTP errors, but never select business outcomes.
- Application services authorize the requested capability, coordinate transactions and call domain operations; avoid reaching into other modules' ORM models.
- Pure domain rules accept explicit time, currency, identifiers and policies, not globals, sessions or network clients.
- Repositories reflect persistence tasks needed by use cases. Avoid generic repositories with twenty methods and no meaningful guarantees.
- Integrations use anti-corruption layers to translate external semantics and error categories.
- Keep business identities stable and avoid database IDs as universal public identifiers without an explicit decision.

## Transaction and concurrency examples
A booking use case must validate the slot and insert under a database-enforced exclusion/unique constraint or a serialization strategy. A read-then-write check is insufficient. Return a domain conflict and map to HTTP 409; provide idempotency for request retry where appropriate.

An invoice total invariant needs a defined precision and currency rounding rule, owned by the domain, exercised with decimal inputs. Do not distribute this calculation across routes, SQL views and frontend JavaScript.

## DDD decision record
Document: bounded context, business owner, invariant, aggregate boundary, source of truth, consistency level, integrations, model evolution, failures, alternatives rejected, and an example adversarial test.

## Red flags
- Namespaces named `services`, `utils` or `common` contain business policy.
- Every entity has an interface/factory merely to satisfy a template.
- A service owns tables but another service joins those tables directly.
- An event carries an entire sensitive ORM row, without minimization or schema version.
- A domain command claims success before dependent asynchronous work is complete when the user expects atomic completion.
