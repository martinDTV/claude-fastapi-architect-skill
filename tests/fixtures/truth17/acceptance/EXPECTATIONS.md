# truth17 / acceptance — held-out expectations (not run during implementation)

Written together with the dev partition, before the v1.7 detectors existed, with different shapes:
a dependency chain of three levels behind a `dict` context, a `needs(scope)` factory, a `Permission(*scopes)`
class, resource authorization as a dependency (`verify_patient_access`) and as a service object (`access.check`),
membership through a router-level dependency, RLS context through `SET LOCAL` text, an audit object holding its
session and HTTP client as attributes, SQS publishing, outbox and inbox tables, a `backoff` decorator without a
bound, and a give-up write inside a retry loop. The rules are the same as in `dev/EXPECTATIONS.md`.

This partition is scored once, at the end, with the previous and the current engine. Failures are reported
as such; the corpus is not edited to make them pass.

## Change log
- (none)
