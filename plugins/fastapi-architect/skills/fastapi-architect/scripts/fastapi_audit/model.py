"""Data model, rule catalogue and constants shared by every auditor module."""

from __future__ import annotations

import hashlib
import platform
import re
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

TOOL_NAME = "audit_fastapi"
TOOL_VERSION = "1.8.0"
# 1.0: base report. 1.1: environment/suppression fields. 1.2: category, fingerprint,
# verification, recommended_test, migration environment, facts/config/comparison blocks.
# 1.3: evidence and call_path per finding, interprocedural block, reliability category.
# 1.4: resolution adds builtin/object/ambiguous; interprocedural adds edges, coverage, uncertainty,
#      limits, attribute_kinds, files_skipped; config adds time_budget_s; ANALYSIS_PARTIAL rule.
# 1.5: top-level `contracts` block (security contract per route); `evidence_state` per finding and
#      `by_evidence_state` in the summary; `contracts` category for declared, opt-in contracts.
# Every field of an earlier schema keeps its name, type and meaning.
SCHEMA_VERSION = "1.6"

EVIDENCE_VERIFIED = "verified"  # pattern and symbols resolved directly from the AST
EVIDENCE_INFERRED = "inferred"  # a type or flow was propagated from callers/dependencies or guessed from names
EVIDENCE_UNKNOWN = "unknown"  # the property could not be resolved; the finding is a question
# v1.7 evidence states (schema 1.5): a single label per finding for reports and CI gates.
STATE_REPRODUCED = "reproduced"  # never produced by static analysis; reserved for runtime records
STATE_STATIC_CONFIRMED = "static_confirmed"  # pattern and symbols resolved from the source
STATE_INFERRED = "inferred"  # a type or flow was propagated; confirm the target
STATE_INSUFFICIENT = "insufficient"  # the property could not be decided statically: a question
STATE_PARTIAL = "partial"  # the analysis of this file/pass did not complete
STATE_SUPPRESSED = "suppressed"  # documented in source; kept for review
EVIDENCE_STATES: Tuple[str, ...] = (STATE_REPRODUCED, STATE_STATIC_CONFIRMED, STATE_INFERRED, STATE_INSUFFICIENT, STATE_PARTIAL, STATE_SUPPRESSED)
PARTIAL_RULES = frozenset({"ANALYSIS_PARTIAL", "FILE_SKIPPED", "PY_SYNTAX"})
PYTHON_VERSION = platform.python_version()

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3}
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}
CONFIDENCE_DOWNGRADE = {"high": "medium", "medium": "low", "low": "low"}

ENV_APPLICATION = "application"
ENV_TEST = "test"
ENV_MIGRATION = "migration"

VERIFICATION_STATIC = "static"  # the pattern itself is the evidence
VERIFICATION_RUNTIME = "needs_runtime_check"  # impact depends on runtime behaviour
VERIFICATION_NOT_STATIC = "not_verifiable_statically"  # property cannot be decided from source

CATEGORIES: Tuple[str, ...] = (
    "async",
    "routes",
    "sqlalchemy",
    "transactions",
    "migrations",
    "security",
    "architecture",
    "reliability",
    "contracts",  # v1.7: declared, opt-in architectural contracts
    "files",
)

DISCLAIMER = (
    "Findings are heuristic signals produced by static analysis of Python source "
    "with the standard-library ast module. They are not demonstrated defects or "
    "vulnerabilities, and the absence of findings is not evidence of correctness. "
    "Verify each finding in context before acting on it; findings marked "
    "needs_runtime_check or not_verifiable_statically require a test or a runtime "
    "observation. The tool does not execute, import or modify the inspected code."
)

# rule -> severity, category, summary, default verification
RULES: Dict[str, Dict[str, str]] = {
    # ---- v1.3 ---------------------------------------------------------------
    "ASYNC_BLOCKING_CALL": {
        "severity": "medium",
        "category": "async",
        "verification": VERIFICATION_RUNTIME,
        "summary": (
            "A potentially blocking call is executed directly inside an async "
            "function. It stalls the event loop for its duration. Review: offload "
            "it, use an async client, or declare the function with plain 'def'."
        ),
    },
    "ASYNCIO_RUN_NESTED": {
        "severity": "high",
        "category": "async",
        "verification": VERIFICATION_STATIC,
        "summary": (
            "asyncio.run() or loop.run_until_complete() is invoked inside an async "
            "function. With a running loop this raises RuntimeError."
        ),
    },
    "ROUTE_NO_RETURN_ANNOTATION": {
        "severity": "info",
        "category": "routes",
        "verification": VERIFICATION_STATIC,
        "summary": (
            "A FastAPI route declares neither a return annotation nor "
            "response_model/response_class, so the response is not validated or "
            "filtered. Informational: a response_model elsewhere may cover it."
        ),
    },
    "PY_SYNTAX": {
        "severity": "low",
        "category": "files",
        "verification": VERIFICATION_STATIC,
        "summary": (
            "The file could not be parsed with the running Python grammar. Other "
            "rules did not run on it."
        ),
    },
    "FILE_SKIPPED": {
        "severity": "info",
        "category": "files",
        "verification": VERIFICATION_STATIC,
        "summary": "The file was not analysed (unreadable or above the size limit).",
    },
    # ---- v1.4 SQLAlchemy sessions and engines ---------------------------------
    "SA_SESSION_MODULE_GLOBAL": {
        "severity": "medium",
        "category": "sqlalchemy",
        "verification": VERIFICATION_STATIC,
        "summary": (
            "A Session or AsyncSession instance is created at module import time and "
            "shared as a global. Sessions are not thread- or task-safe and hold "
            "connections and identity state across requests. A sessionmaker / "
            "async_sessionmaker global (a factory) is fine; an instance is not."
        ),
    },
    "SA_SESSION_SHARED_CONCURRENTLY": {
        "severity": "high",
        "category": "sqlalchemy",
        "verification": VERIFICATION_RUNTIME,
        "summary": (
            "The same session object is handed to several concurrently scheduled "
            "coroutines or threads (asyncio.gather, create_task, task groups, "
            "executors). AsyncSession and Session must not be used concurrently; "
            "give each task its own session."
        ),
    },
    "SA_ASYNC_SESSION_NOT_AWAITED": {
        "severity": "high",
        "category": "sqlalchemy",
        "verification": VERIFICATION_STATIC,
        "summary": (
            "A coroutine method of an AsyncSession (execute, commit, flush, get, "
            "scalar...) is called without await, so it never runs."
        ),
    },
    "SA_SYNC_SESSION_IN_ASYNC_DEF": {
        "severity": "medium",
        "category": "sqlalchemy",
        "verification": VERIFICATION_RUNTIME,
        "summary": (
            "A synchronous ORM session performs database I/O directly inside an "
            "async function, blocking the event loop; or the receiver type is "
            "unknown and the call may be a missing await on an AsyncSession."
        ),
    },
    "SA_ENGINE_CREATED_PER_CALL": {
        "severity": "medium",
        "category": "sqlalchemy",
        "verification": VERIFICATION_STATIC,
        "summary": (
            "create_engine / create_async_engine is called inside a request handler "
            "or an ordinary function, creating a new connection pool per call. "
            "Create the engine once (lifespan or cached factory)."
        ),
    },
    "SA_CREATE_ALL_AT_RUNTIME": {
        "severity": "medium",
        "category": "sqlalchemy",
        "verification": VERIFICATION_RUNTIME,
        "summary": (
            "metadata.create_all() is called from application code. Acceptable for "
            "prototypes and tests; production schemas should evolve through Alembic "
            "migrations."
        ),
    },
    "SA_SESSION_NOT_CLOSED": {
        "severity": "medium",
        "category": "sqlalchemy",
        "verification": VERIFICATION_STATIC,
        "summary": (
            "A session is created from a factory without a context manager and is "
            "neither closed nor returned to a caller in this function, so its "
            "connection can leak (a yield-based dependency must close it in finally)."
        ),
    },
    # ---- v1.4 transactions and concurrency -----------------------------------
    "TX_COMMIT_IN_LOOP": {
        "severity": "medium",
        "category": "transactions",
        "verification": VERIFICATION_RUNTIME,
        "summary": (
            "commit() is called inside a loop: one transaction per iteration, "
            "partial state if a later iteration fails, and round-trip overhead. "
            "Review whether the loop should be one transaction or deliberate batches."
        ),
    },
    "TX_EXTERNAL_CALL_IN_TRANSACTION": {
        "severity": "medium",
        "category": "transactions",
        "verification": VERIFICATION_RUNTIME,
        "summary": (
            "An HTTP call or a sleep happens inside an explicit transaction block "
            "(session.begin()/engine.begin()), holding locks and a pooled connection "
            "for the duration of network I/O. Move the call outside or use an outbox."
        ),
    },
    "TX_READ_MODIFY_WRITE": {
        "severity": "medium",
        "category": "transactions",
        "verification": VERIFICATION_NOT_STATIC,
        "summary": (
            "A row is loaded, a field is updated in Python from its previous value, "
            "and the result is written back without an evident lock or optimistic "
            "version. Two concurrent transactions can lose one update. Reproduce "
            "with two sessions before deciding on FOR UPDATE, version_id_col, an "
            "atomic UPDATE or a constraint."
        ),
    },
    "TX_COMMIT_WITHOUT_ROLLBACK": {
        "severity": "medium",
        "category": "transactions",
        "verification": VERIFICATION_STATIC,
        "summary": (
            "commit() runs inside a try whose except handler neither rolls back nor "
            "re-raises, leaving the session in a failed transaction state."
        ),
    },
    # ---- v1.4 Alembic migrations --------------------------------------------
    "MIG_DROP_TABLE": {
        "severity": "high",
        "category": "migrations",
        "verification": VERIFICATION_RUNTIME,
        "summary": (
            "The migration drops a table. Data is lost and downgrade cannot restore "
            "it. Confirm the expand-contract cycle finished (no reader or writer "
            "left) and that a backup or archive exists."
        ),
    },
    "MIG_DROP_COLUMN": {
        "severity": "high",
        "category": "migrations",
        "verification": VERIFICATION_RUNTIME,
        "summary": (
            "The migration drops a column. Running code that still reads or writes "
            "it breaks during a rolling deploy, and the data is gone. Confirm the "
            "contract phase of expand-contract and the rollback plan."
        ),
    },
    "MIG_DROP_CONSTRAINT": {
        "severity": "medium",
        "category": "migrations",
        "verification": VERIFICATION_RUNTIME,
        "summary": (
            "The migration drops a constraint or index. Invariants or query plans "
            "may change; confirm nothing relies on it and how it is restored."
        ),
    },
    "MIG_NOT_NULL_WITHOUT_DEFAULT": {
        "severity": "medium",
        "category": "migrations",
        "verification": VERIFICATION_RUNTIME,
        "summary": (
            "A NOT NULL column is added or enforced without a server_default or a "
            "visible backfill. On a table with rows this fails or needs a full "
            "rewrite; use add-nullable, backfill, then SET NOT NULL."
        ),
    },
    "MIG_TYPE_CHANGE": {
        "severity": "medium",
        "category": "migrations",
        "verification": VERIFICATION_RUNTIME,
        "summary": (
            "The migration changes a column type. Depending on the conversion this "
            "rewrites the table under an exclusive lock and can be incompatible with "
            "existing data or running code."
        ),
    },
    "MIG_INDEX_NOT_CONCURRENT": {
        "severity": "medium",
        "category": "migrations",
        "verification": VERIFICATION_RUNTIME,
        "summary": (
            "An index is created without postgresql_concurrently=True. On PostgreSQL "
            "a plain CREATE INDEX blocks writes on the table for its duration; "
            "concurrent creation needs an autocommit block and is not transactional."
        ),
    },
    "MIG_DDL_WITH_DML": {
        "severity": "medium",
        "category": "migrations",
        "verification": VERIFICATION_RUNTIME,
        "summary": (
            "The same migration mixes schema changes with data transformations. "
            "Large backfills inside a DDL transaction hold locks and make the step "
            "hard to retry; split schema and data steps."
        ),
    },
    "MIG_DOWNGRADE_NOOP": {
        "severity": "low",
        "category": "migrations",
        "verification": VERIFICATION_STATIC,
        "summary": (
            "downgrade() is empty or raises, so this migration cannot be rolled "
            "back with Alembic. Acceptable when documented; roll-forward is then "
            "the only path."
        ),
    },
    "MIG_RAW_SQL": {
        "severity": "low",
        "category": "migrations",
        "verification": VERIFICATION_RUNTIME,
        "summary": (
            "Raw SQL in a migration (ALTER, LOCK, VACUUM, CLUSTER...). Review its "
            "lock level, duration and reversibility explicitly."
        ),
    },
    # ---- v1.4 security and multi-tenancy -------------------------------------
    "SEC_TENANT_ID_FROM_REQUEST": {
        "severity": "high",
        "category": "security",
        "verification": VERIFICATION_NOT_STATIC,
        "summary": (
            "A tenant/organisation identifier arrives as request input and is used "
            "by the handler while no authentication or membership dependency is "
            "visible on the handler, its router or the application. Whether a "
            "membership check exists elsewhere cannot be decided statically."
        ),
    },
    "SEC_RLS_SET_NOT_LOCAL": {
        "severity": "medium",
        "category": "security",
        "verification": VERIFICATION_RUNTIME,
        "summary": (
            "A session-level SET (or set_config(..., false)) assigns a tenant/role "
            "setting. With connection pooling the value survives the transaction and "
            "leaks to the next request. Use SET LOCAL or set_config(..., true) inside "
            "the transaction, and reset on checkout."
        ),
    },
    "SEC_ROUTE_WITHOUT_AUTH_SIGNAL": {
        "severity": "info",
        "category": "security",
        "verification": VERIFICATION_NOT_STATIC,
        "summary": (
            "No authentication or authorization dependency is visible on this route, "
            "its router or the application. Protection may live in middleware, a "
            "gateway or a composed dependency; confirm with a negative test."
        ),
    },
    # ---- v1.4 architecture boundaries ----------------------------------------
    "ARCH_SERVICE_IMPORTS_FASTAPI": {
        "severity": "medium",
        "category": "architecture",
        "verification": VERIFICATION_STATIC,
        "summary": (
            "A service, use-case or repository module imports FastAPI/Starlette. "
            "Business code bound to the web framework cannot be reused from workers "
            "or CLIs and tends to raise HTTPException from the wrong layer."
        ),
    },
    "ARCH_DOMAIN_IMPORTS_INFRA": {
        "severity": "medium",
        "category": "architecture",
        "verification": VERIFICATION_STATIC,
        "summary": (
            "A domain module imports an infrastructure library (ORM, HTTP client, "
            "broker, cloud SDK). Pure domain code should depend on ports, not on "
            "concrete implementations."
        ),
    },
    "ARCH_REPOSITORY_IMPORTS_SERVICE": {
        "severity": "medium",
        "category": "architecture",
        "verification": VERIFICATION_STATIC,
        "summary": (
            "A repository module imports a service module, inverting the dependency "
            "direction router -> service -> repository."
        ),
    },
    "ARCH_CROSS_MODULE_INTERNAL_IMPORT": {
        "severity": "medium",
        "category": "architecture",
        "verification": VERIFICATION_STATIC,
        "summary": (
            "A business module imports another module's internals (models, "
            "repository, service) instead of its public facade. The coupling blocks "
            "later extraction and bypasses the owning module's rules."
        ),
    },
    "ARCH_CORE_IMPORTS_MODULE": {
        "severity": "medium",
        "category": "architecture",
        "verification": VERIFICATION_STATIC,
        "summary": (
            "Infrastructure code (core/shared/utils) imports a business module. "
            "core must not know about business modules; move the logic to the "
            "owning module or invert the dependency."
        ),
    },
    "ARCH_IMPORT_CYCLE": {
        "severity": "medium",
        "category": "architecture",
        "verification": VERIFICATION_STATIC,
        "summary": (
            "Module-level imports form a cycle between project files. Cycles make "
            "import order fragile and signal a missing boundary."
        ),
    },
    "ARCH_IMPORT_LINTER_PRESENT": {
        "severity": "info",
        "category": "architecture",
        "verification": VERIFICATION_STATIC,
        "summary": (
            "import-linter contracts exist in this project. Treat them as the source "
            "of truth for boundaries: run lint-imports; the heuristic architecture "
            "findings here are secondary and reported with lower confidence."
        ),
    },
}

RULES.update(
    {
        # ---- v1.5 interprocedural ------------------------------------------
        "ASYNC_BLOCKING_CALL_INDIRECT": {
            "severity": "medium",
            "category": "async",
            "verification": VERIFICATION_RUNTIME,
            "summary": (
                "An async function calls a synchronous project helper that, along a "
                "resolved call path, performs a blocking call or synchronous database "
                "I/O. The event loop stalls just as with a direct call; the call path "
                "is reported as evidence."
            ),
        },
        "TX_NESTED_BEGIN": {
            "severity": "high",
            "category": "transactions",
            "verification": VERIFICATION_STATIC,
            "summary": (
                "session.begin() is opened inside a block that already began a "
                "transaction on the same session. SQLAlchemy raises InvalidRequestError "
                "('a transaction is already begun'); use begin_nested() for a savepoint "
                "or let the outer transaction own the work."
            ),
        },
        "SA_ASYNC_LAZY_LOAD_RISK": {
            "severity": "medium",
            "category": "sqlalchemy",
            "verification": VERIFICATION_RUNTIME,
            "summary": (
                "An ORM relationship with an implicit lazy strategy is accessed in an "
                "async function after the object was loaded without an explicit "
                "loader option. Under AsyncSession the implicit lazy load raises "
                "MissingGreenlet; with a closed session it raises DetachedInstanceError. "
                "Use selectinload/joinedload, lazy='selectin', or lazy='raise' as a guard."
            ),
        },
        "RETRY_NON_IDEMPOTENT_WRITE": {
            "severity": "medium",
            "category": "reliability",
            "verification": VERIFICATION_RUNTIME,
            "summary": (
                "A retried operation (retry decorator or hand-written retry loop) "
                "performs a database or HTTP write and no idempotency signal is visible "
                "(idempotency key, dedup/request id, ON CONFLICT). After a lost response "
                "the write can execute twice. Verify with a fault-injection test."
            ),
        },
        "BG_REQUEST_SCOPED_RESOURCE": {
            "severity": "high",
            "category": "reliability",
            "verification": VERIFICATION_STATIC,
            "summary": (
                "A request-scoped session provided by a dependency is handed to a "
                "background task (BackgroundTasks.add_task or a task created in the "
                "handler). The dependency closes the session when the response is sent, "
                "while the task is still running; create a session inside the task."
            ),
        },
        "BG_TASK_CONTEXT_UNVERIFIED": {
            "severity": "info",
            "category": "reliability",
            "verification": VERIFICATION_NOT_STATIC,
            "summary": (
                "A background task writes to the database or an external system and "
                "neither receives tenant/principal context nor recreates it visibly "
                "(own session, tenant setting). Whether authorization and tenant scope "
                "apply inside the task cannot be decided statically."
            ),
        },
    }
)

RULES.update(
    {
        # ---- v1.6 ---------------------------------------------------------------
        "ASYNC_TASK_REFERENCE_DROPPED": {
            "severity": "medium",
            "category": "async",
            "verification": VERIFICATION_STATIC,
            "summary": (
                "asyncio.create_task()/ensure_future() is called and its result is dropped. The event "
                "loop keeps only a weak reference to tasks, so a task with no other reference can be "
                "garbage-collected mid-execution (Python docs: 'Save a reference to the result of this "
                "function'). Keep the task in a collection, await it, or use a TaskGroup."
            ),
        },
        "RETRY_UNBOUNDED": {
            "severity": "medium",
            "category": "reliability",
            "verification": VERIFICATION_STATIC,
            "summary": (
                "A retry has no stop condition: a tenacity/retrying/backoff decorator without a stop or "
                "max-attempts argument (their default is to retry forever), or a 'while True' retry loop "
                "without an attempt counter. A persistent failure turns into a hung request or worker; "
                "bound attempts and total time, with backoff and jitter."
            ),
        },
        "REL_HTTP_CALL_WITHOUT_TIMEOUT": {
            "severity": "medium",
            "category": "reliability",
            "verification": VERIFICATION_STATIC,
            "summary": (
                "An outbound HTTP call can wait forever: requests.* without timeout= (requests has no "
                "default timeout), urlopen without timeout, or an httpx client built with timeout=None. "
                "A stalled dependency then holds the worker, the pooled connection or the event loop task "
                "indefinitely. Set an explicit timeout and handle it."
            ),
        },
        "REL_DUAL_WRITE_WITHOUT_OUTBOX": {
            "severity": "low",
            "category": "reliability",
            "verification": VERIFICATION_RUNTIME,
            "summary": (
                "One function commits a database change and publishes a message to a broker or queue, "
                "with no outbox/inbox table visible. The two writes are not atomic: a crash between them "
                "loses the message or publishes a change that was rolled back. Record the event in an "
                "outbox row inside the same transaction and relay it from a worker."
            ),
        },
        "SEC_TENANT_NOT_BOUND_TO_PRINCIPAL": {
            "severity": "medium",
            "category": "security",
            "verification": VERIFICATION_NOT_STATIC,
            "summary": (
                "A route is authenticated, takes a tenant identifier from the request and uses it in "
                "project calls, but no visible step binds that tenant to the authenticated principal: the "
                "authentication dependency chain has no tenant parameter and the principal does not flow "
                "where the tenant goes. If membership is checked elsewhere this is a false positive; "
                "otherwise any authenticated caller can address any tenant. Not verifiable statically."
            ),
        },
        "SEC_PRIVILEGE_CHANGE_UNVERIFIED": {
            "severity": "medium",
            "category": "security",
            "verification": VERIFICATION_NOT_STATIC,
            "summary": (
                "A route changes roles, permissions or admin flags (attribute assignment or a grant/"
                "revoke/promote call) and only a generic authentication signal is visible; no dependency "
                "or parameter names an admin/owner/role/permission policy. Whether the caller is allowed "
                "to change privileges cannot be decided statically; confirm with a negative test as a "
                "plain member."
            ),
        },
        "ANALYSIS_PARTIAL": {
            "severity": "info",
            "category": "files",
            "verification": VERIFICATION_STATIC,
            "summary": (
                "A rule module hit a resource limit (recursion or memory) on this file and reported "
                "nothing for it. The remaining rule modules ran; the file's analysis is partial."
            ),
        },
    }
)

RULES.update(
    {
        # ---- v1.7 ---------------------------------------------------------------
        "SEC_RESOURCE_AUTHZ_UNVERIFIED": {
            "severity": "medium",
            "category": "security",
            "verification": VERIFICATION_NOT_STATIC,
            "summary": (
                "An authenticated route acts on a resource identified by the request and hands the id "
                "to project code without the principal; no role, scope, membership or resource dependency, "
                "policy call or comparison relates the resource to the caller in the visible code. Whether "
                "ownership is enforced elsewhere cannot be decided statically."
            ),
        },
        "SEC_RLS_CONTEXT_FROM_REQUEST": {
            "severity": "medium",
            "category": "security",
            "verification": VERIFICATION_RUNTIME,
            "summary": (
                "The row-level-security tenant context (set_config / SET LOCAL) is set from a tenant id "
                "taken from the request in an authenticated route that does not bind that tenant to the "
                "principal. The check exists but is not tied to who is calling: the policies show whatever "
                "tenant the caller names."
            ),
        },
        "ASYNC_COROUTINE_NOT_AWAITED": {
            "severity": "high",
            "category": "async",
            "verification": VERIFICATION_STATIC,
            "summary": (
                "A project coroutine function is called without await and its result is neither awaited "
                "later, scheduled (gather, create_task, TaskGroup) nor handed to a caller that awaits it. "
                "The body never runs; Python warns 'coroutine was never awaited'."
            ),
        },
        "ASYNC_LOCK_PER_CALL": {
            "severity": "medium",
            "category": "async",
            "verification": VERIFICATION_STATIC,
            "summary": (
                "A lock is created inside the function that acquires it, so every call gets a fresh lock "
                "and nothing is mutually excluded. Create the lock once (module or object attribute) and "
                "share it."
            ),
        },
        "REL_HTTP_CLIENT_PER_CALL": {
            "severity": "low",
            "category": "reliability",
            "verification": VERIFICATION_RUNTIME,
            "summary": (
                "An HTTP client is built inside a function for a single use and neither managed with "
                "`with` nor closed: no connection reuse, and sockets or pooled connections leak until "
                "garbage collection. Share one client per application (lifespan) or manage it with `with`."
            ),
        },
    }
)

RULES.update(
    {
        # ---- v1.7 declared contracts (opt-in through .audit-fastapi.json) and consumers ---
        "CONTRACT_ROUTE_AUTH_REQUIRED": {
            "severity": "high",
            "category": "contracts",
            "verification": VERIFICATION_STATIC,
            "summary": (
                "The project declares (contracts.route_auth_required) that routes under these paths "
                "must be authenticated, and this route shows no authentication and is not declared public."
            ),
        },
        "CONTRACT_MUTATION_AUTHZ_REQUIRED": {
            "severity": "medium",
            "category": "contracts",
            "verification": VERIFICATION_STATIC,
            "summary": (
                "The project declares (contracts.mutation_authorization_required) that mutating routes "
                "under these paths must show an authorization kind, and this one shows only authentication."
            ),
        },
        "CONTRACT_IMPORT_FORBIDDEN": {
            "severity": "medium",
            "category": "contracts",
            "verification": VERIFICATION_STATIC,
            "summary": (
                "The project declares (contracts.forbidden_imports) that these modules must not import "
                "those modules, and this file does."
            ),
        },
        "CONTRACT_COMMIT_OUTSIDE_LAYER": {
            "severity": "medium",
            "category": "contracts",
            "verification": VERIFICATION_STATIC,
            "summary": (
                "The project declares (contracts.commit_layers) which layers own transactions, and this "
                "file, classified in another layer, commits."
            ),
        },
        "REL_CONSUMER_WITHOUT_INBOX": {
            "severity": "low",
            "category": "reliability",
            "verification": VERIFICATION_NOT_STATIC,
            "summary": (
                "A message/event consumer writes to the database and shows no signal that recognises a "
                "redelivery (inbox table, processed-ids insert with ON CONFLICT, idempotency key). Brokers "
                "deliver at least once; whether a duplicate is applied twice cannot be decided statically."
            ),
        },
    }
)

# ---- v1.8 transactional precision ------------------------------------------
RULES.update(
    {
        "TX_COMMIT_INSIDE_BEGIN_BLOCK": {
            "severity": "high",
            "category": "transactions",
            "verification": VERIFICATION_STATIC,
            "summary": (
                "commit() is called inside a 'with session.begin()' block. The context manager owns "
                "that transaction and commits it on exit; the explicit commit closes it early, and any "
                "statement after it in the block raises InvalidRequestError ('Can't operate on closed "
                "transaction inside context manager'; SQLAlchemy 2.0, reproduced on PostgreSQL). When the "
                "commit is the last statement it is redundant (medium). Remove it or drop the begin() block."
            ),
        },
    }
)

RULE_CATEGORIES: Dict[str, Tuple[str, ...]] = {
    category: tuple(sorted(r for r, meta in RULES.items() if meta["category"] == category))
    for category in CATEGORIES
}

_DIGITS = re.compile(r"\d+")


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    confidence: str
    file: str
    line: int
    column: int
    message: str
    symbol: Optional[str] = None
    function: Optional[str] = None
    environment: str = ENV_APPLICATION  # schema 1.1
    suppressed: bool = False  # schema 1.1
    suppression: Optional[str] = None  # schema 1.1
    category: str = "files"  # schema 1.2
    verification: str = VERIFICATION_STATIC  # schema 1.2
    recommended_test: Optional[str] = None  # schema 1.2
    evidence: str = EVIDENCE_VERIFIED  # schema 1.3
    call_path: Optional[Tuple[str, ...]] = None  # schema 1.3: caller -> ... -> symbol
    evidence_state: str = ""  # schema 1.5: one of EVIDENCE_STATES (set by the engine after classification)
    details: Optional[Dict[str, object]] = None  # schema 1.6: rule-specific structured facts (loop_kind, ...)
    remediation: Optional[Dict[str, object]] = None  # schema 1.6: structured, non-applied remediation proposal

    @property
    def fingerprint(self) -> str:
        """Stable identity across line shifts: rule, file, function, symbol, message shape."""
        raw = "|".join(
            [
                self.rule,
                self.file,
                self.function or "",
                self.symbol or "",
                _DIGITS.sub("#", self.message),
            ]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> Dict[str, object]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "confidence": self.confidence,
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "message": self.message,
            "symbol": self.symbol,
            "function": self.function,
            "environment": self.environment,
            "suppressed": self.suppressed,
            "suppression": self.suppression,
            "category": self.category,
            "verification": self.verification,
            "recommended_test": self.recommended_test,
            "fingerprint": self.fingerprint,
            "evidence": self.evidence,
            "call_path": list(self.call_path) if self.call_path else None,
            "evidence_state": self.evidence_state or evidence_state_for(self),
            "details": dict(self.details) if self.details else None,  # schema 1.6
            "remediation": dict(self.remediation) if self.remediation else None,  # schema 1.6
        }

    def sort_key(self) -> Tuple[str, int, int, str, str, str]:
        return (self.file, self.line, self.column, self.rule, self.symbol or "", self.message)


def evidence_state_for(finding: "Finding") -> str:
    """Single label for a finding: suppressed > partial > insufficient > inferred > static_confirmed.
    ``reproduced`` is never produced here: static analysis cannot execute anything."""
    if finding.suppressed:
        return STATE_SUPPRESSED
    if finding.rule in PARTIAL_RULES:
        return STATE_PARTIAL
    if finding.evidence == EVIDENCE_UNKNOWN or finding.verification == VERIFICATION_NOT_STATIC:
        return STATE_INSUFFICIENT
    if finding.evidence == EVIDENCE_INFERRED:
        return STATE_INFERRED
    return STATE_STATIC_CONFIRMED


def new_finding(
    rule: str,
    file: str,
    line: int,
    column: int,
    message: str,
    *,
    confidence: str,
    symbol: Optional[str] = None,
    function: Optional[str] = None,
    environment: str = ENV_APPLICATION,
    verification: Optional[str] = None,
    recommended_test: Optional[str] = None,
    severity: Optional[str] = None,
    evidence: str = EVIDENCE_VERIFIED,
    call_path: Optional[Sequence[str]] = None,
    details: Optional[Dict[str, object]] = None,
) -> Finding:
    """Build a Finding with severity, category and default verification from the catalogue."""
    meta = RULES[rule]
    return Finding(
        rule=rule,
        severity=severity or meta["severity"],
        confidence=confidence,
        file=file,
        line=int(line),
        column=int(column),
        message=message,
        symbol=symbol,
        function=function,
        environment=environment,
        category=meta["category"],
        verification=verification or meta["verification"],
        recommended_test=recommended_test,
        evidence=evidence,
        call_path=tuple(call_path) if call_path else None,
        details=dict(details) if details else None,
    )
