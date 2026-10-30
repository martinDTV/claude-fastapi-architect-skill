"""v1.8: call-classification precision (local origins, callbacks, attribute chains, mixins, hooks, external
bases), structural classification of commit loops, commit inside ``begin()``, structured remediation proposals,
the report comparison tool and schema 1.6. The ``truth18/dev`` corpus is scored in-process; the ``acceptance``
partition is scored only by ``scripts/audit_precision.py`` at release time and never here."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import sys
import textwrap
import unittest
from copy import deepcopy
from pathlib import Path

from _audit_support import FIXTURES, REPO_ROOT, SKILL_SCRIPTS, ProjectCase, audit, audit_fixture, lines_of

from fastapi_audit.report import compare_reports  # noqa: E402  (SKILL_SCRIPTS is on sys.path via _audit_support)
from fastapi_audit.rules.transactions import classify_commit_loop  # noqa: E402
from fastapi_audit.remediation import REMEDIATIONS  # noqa: E402


def _load_precision():
    spec = importlib.util.spec_from_file_location("audit_precision_v18", REPO_ROOT / "scripts" / "audit_precision.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


precision = _load_precision()
DEV = FIXTURES / "truth18" / "dev"
DIFF_SHIM = REPO_ROOT / "scripts" / "audit_diff.py"

SESSION_HEADER = "from sqlalchemy.ext.asyncio import AsyncSession\n"


class DevCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.truth = precision.load_truth(str(DEV))
        cls.score = precision.score(cls.truth, precision.collect_run(str(DEV)))

    def test_every_expectation_holds(self) -> None:
        self.assertGreaterEqual(len(self.truth["expected"]), 7)
        self.assertGreaterEqual(len(self.truth["absent"]), 8)
        self.assertEqual(self.score["false_negatives"], [])
        self.assertEqual(self.score["false_positives"], [])
        self.assertEqual(self.score["evidence_mismatches"], [])
        self.assertEqual((self.score["precision"], self.score["recall"]), (1.0, 1.0))

    def test_expected_edges_and_unresolved_statuses(self) -> None:
        edges, unresolved = self.score["edges"], self.score["unresolved"]
        self.assertEqual(edges["expected"], 6)
        self.assertEqual(edges["missing"], [])
        self.assertEqual(unresolved["expected"], 16)
        self.assertEqual(unresolved["wrong"], [])

    def test_inferred_evidence_is_declared_not_hidden(self) -> None:
        counts = self.score["evidence_counts"]
        self.assertEqual(counts.get("inferred", 0), 3)  # mixin kind, callback edge, bound-method callback
        self.assertEqual(counts.get("unknown", 0), 0)


# --------------------------------------------------------------------------- #
# P1: where locals come from
# --------------------------------------------------------------------------- #


class ClassificationTests(ProjectCase):
    def stats(self):
        return self.run_audit()["interprocedural"]

    def test_library_results_are_external_not_dynamic(self) -> None:
        self.write("app/m.py", '''
            import json
            import httpx

            async def fetch(client: httpx.AsyncClient) -> dict:
                response = await client.get("/x")
                data = response.json()
                return data.copy()

            def parse(raw: str) -> str:
                payload = json.loads(raw)
                return payload.get("a")
        ''')
        stats = self.stats()
        self.assertEqual(stats["resolution"]["dynamic"], 0)
        self.assertEqual(stats["uncertainty"]["dynamic_by_origin"], {})
        self.assertGreaterEqual(stats["resolution"]["external"], 5)

    def test_unpacked_builtin_results_are_builtin(self) -> None:
        self.write("app/m.py", '''
            def split(line: str) -> str:
                name, value = line.split(":", 1)
                return name.strip() + value.strip()
        ''')
        stats = self.stats()
        self.assertEqual(stats["resolution"]["dynamic"], 0)
        self.assertEqual(stats["resolution"]["builtin"], 3)

    def test_loop_targets_and_comprehensions_stay_dynamic_with_an_origin(self) -> None:
        self.write("app/m.py", '''
            def a(items: list[str]) -> list[str]:
                out = []
                for item in items:
                    out.append(item.upper())
                return out

            def b(items: list[str]) -> list[str]:
                return [item.lower() for item in items]
        ''')
        stats = self.stats()
        self.assertEqual(stats["uncertainty"]["dynamic_by_origin"], {"comprehension": 1, "loop_target": 1})
        self.assertEqual(stats["resolution"]["project"], 0)  # no invented project edge

    def test_closure_inherits_the_declared_session_kind(self) -> None:
        self.write("app/m.py", '''
            from sqlalchemy.ext.asyncio import AsyncSession
            async def outer(session: AsyncSession) -> object:
                async def inner():
                    return session.execute("select 1")
                return await inner()
        ''')
        report = self.run_audit()
        items = [f for f in report["findings"] if f["rule"] == "SA_ASYNC_SESSION_NOT_AWAITED"]
        self.assertEqual([(f["line"], f["evidence"], f["confidence"]) for f in items], [(4, "verified", "high")])

    def test_closures_inside_methods_and_asgi_handlers(self) -> None:
        """Found by the acceptance corpus at its first scoring (then fixed and re-scored, see RELEASE-NOTES-v1.8.md):
        a closure inside a method reads the method's `self`, and a closure inside an ASGI handler awaits the outer `send`."""
        self.write("app/m.py", '''
            from sqlalchemy import text
            from sqlalchemy.ext.asyncio import AsyncSession
            from starlette.types import ASGIApp, Message, Receive, Scope, Send

            class Syncer:
                def __init__(self, session: AsyncSession) -> None:
                    self._session = session

                def helper_in_closure(self) -> int:
                    def count() -> int:
                        self._session.execute(text("select 1"))
                        return 1
                    return count()

            class Timing:
                def __init__(self, app: ASGIApp) -> None:
                    self.app = app

                async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
                    async def send_wrapper(message: Message) -> None:
                        await send(message)
                    await self.app(scope, receive, send_wrapper)
        ''')
        report = self.run_audit()
        items = [f for f in report["findings"] if f["rule"] == "SA_ASYNC_SESSION_NOT_AWAITED"]
        self.assertEqual([(f["line"], f["evidence"], f["function"]) for f in items], [(11, "verified", "Syncer.helper_in_closure.count")])
        stats = report["interprocedural"]
        self.assertEqual(stats["uncertainty"]["framework_callbacks"], 1)
        self.assertEqual(stats["uncertainty"]["dynamic_by_origin"], {"nested_function": 1})  # `count()` itself: calls to nested defs are not followed

    def test_project_return_annotations_classify_the_result(self) -> None:
        self.write("app/repo.py", '''
            import time

            class Account:
                def describe(self) -> str:
                    time.sleep(1)
                    return "x"

            def get_account(i: int) -> Account:
                return Account()

            def list_rows() -> list[dict]:
                return []

            def label() -> str:
                return "a"
        ''')
        self.write("app/svc.py", '''
            from . import repo

            async def project_return() -> str:
                account = repo.get_account(1)
                return account.describe()

            async def builtin_returns() -> int:
                rows = repo.list_rows()
                text = repo.label()
                return rows.count({}) + len(text.upper())
        ''')
        report = self.run_audit()
        stats = report["interprocedural"]
        self.assertEqual(stats["edges"]["return"], 1)
        self.assertEqual(stats["resolution"]["dynamic"], 0)
        self.assertEqual(lines_of(report, "ASYNC_BLOCKING_CALL_INDIRECT", "app/svc.py"), [5])

    def test_untyped_parameters_are_an_origin_and_an_unresolved_callback(self) -> None:
        self.write("app/m.py", '''
            def f(x):
                return x.run()

            def g(cb):
                cb()
        ''')
        stats = self.stats()
        self.assertEqual(stats["uncertainty"]["dynamic_by_origin"], {"callback_unresolved": 1, "untyped_param": 1})
        self.assertEqual(stats["uncertainty"]["callback_params_unresolved"], 1)
        self.assertEqual(stats["uncertainty"]["callback_sites_unresolved"], 1)

    def test_coverage_shares_sum_to_one(self) -> None:
        stats = audit_fixture("truth18/dev")["interprocedural"]
        cov = stats["coverage"]
        self.assertAlmostEqual(cov["project_site_ratio"] + cov["external_site_ratio"] + cov["unresolved_site_ratio"], 1.0, places=3)
        self.assertEqual(cov["resolvable_sites"], stats["resolution"]["project"] + stats["resolution"]["not_found"] + stats["resolution"]["ambiguous"])
        self.assertEqual(sum(stats["uncertainty"]["dynamic_by_origin"].values()), stats["resolution"]["dynamic"])


# --------------------------------------------------------------------------- #
# P2: callbacks
# --------------------------------------------------------------------------- #


CALLBACK_MODULE = '''
import time
from collections.abc import Callable

def slow(x: int) -> int:
    time.sleep(1)
    return x

def fast(x: int) -> int:
    return x

def run_with(handler: Callable[[int], int], x: int) -> int:
    return handler(x)
'''


class CallbackTests(ProjectCase):
    def test_asgi_callbacks_are_framework_supplied_not_unresolved(self) -> None:
        self.write("app/mw.py", '''
            from starlette.types import ASGIApp, Receive, Scope, Send

            class Limit:
                def __init__(self, app: ASGIApp) -> None:
                    self.app = app

                async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
                    message = await receive()
                    if message.get("more_body"):
                        await send({"type": "http.response.start", "status": 413})
                    await self.app(scope, receive, send)

            class Untyped:
                async def __call__(self, scope, receive, send):
                    await send({"type": "http.response.start"})
        ''')
        stats = self.run_audit()["interprocedural"]
        self.assertEqual(stats["uncertainty"]["framework_callbacks"], 3)
        self.assertEqual(stats["uncertainty"]["callback_params_unresolved"], 0)
        self.assertEqual(stats["resolution"]["dynamic"], 0)

    def test_one_provider_resolves_including_a_forwarding_lambda(self) -> None:
        self.write("app/cb.py", CALLBACK_MODULE + textwrap.dedent('''
            async def route_like() -> int:
                return run_with(slow, 1)

            def run_lambda(x: int) -> int:
                return run_with(lambda v: slow(v), x)
        '''))
        report = self.run_audit()
        self.assertEqual(report["interprocedural"]["edges"]["callback"], 1)
        items = [f for f in report["findings"] if f["rule"] == "ASYNC_BLOCKING_CALL_INDIRECT"]
        self.assertEqual([(f["line"], f["evidence"]) for f in items], [(15, "inferred")])
        self.assertEqual(items[0]["call_path"], ["app.cb.route_like", "app.cb.run_with", "app.cb.slow", "time.sleep"])

    def test_two_providers_stay_ambiguous_and_silent(self) -> None:
        self.write("app/cb.py", CALLBACK_MODULE + textwrap.dedent('''
            async def ambiguous_route() -> int:
                return run_with(slow, 1) + run_with(fast, 2)
        '''))
        report = self.run_audit()
        self.assertEqual(lines_of(report, "ASYNC_BLOCKING_CALL_INDIRECT"), [])
        self.assertEqual(report["interprocedural"]["uncertainty"]["ambiguous_sites"], 1)

    def test_a_lambda_that_does_more_than_forward_is_not_a_provider(self) -> None:
        self.write("app/cb.py", CALLBACK_MODULE + textwrap.dedent('''
            async def route_like() -> int:
                return run_with(lambda v: slow(v) + 1, 1)
        '''))
        report = self.run_audit()
        self.assertEqual(lines_of(report, "ASYNC_BLOCKING_CALL_INDIRECT"), [])
        self.assertEqual(report["interprocedural"]["uncertainty"]["callback_params_unresolved"], 1)

    def test_bound_method_references_resolve(self) -> None:
        self.write("app/cb.py", '''
            import time
            from collections.abc import Callable

            def apply(fn: Callable[[int], int], v: int) -> int:
                return fn(v)

            class Worker:
                def run(self, x: int) -> int:
                    time.sleep(1)
                    return x

            class Service:
                def handle(self, x: int) -> int:
                    time.sleep(1)
                    return x

                async def dispatch(self) -> int:
                    return apply(self.handle, 1)

            async def schedule(worker: Worker) -> int:
                return apply(worker.run, 2)
        ''')
        report = self.run_audit()
        # two different targets reach `fn`: the site is ambiguous, nothing is reported
        self.assertEqual(lines_of(report, "ASYNC_BLOCKING_CALL_INDIRECT"), [])
        self.assertEqual(report["interprocedural"]["uncertainty"]["ambiguous_sites"], 1)
        self.write("app/cb.py", '''
            import time
            from collections.abc import Callable

            def apply(fn: Callable[[int], int], v: int) -> int:
                return fn(v)

            class Service:
                def handle(self, x: int) -> int:
                    time.sleep(1)
                    return x

                async def dispatch(self) -> int:
                    return apply(self.handle, 1)
        ''')
        report = self.run_audit()
        items = [f for f in report["findings"] if f["rule"] == "ASYNC_BLOCKING_CALL_INDIRECT"]
        self.assertEqual([(f["line"], f["evidence"]) for f in items], [(13, "inferred")])


# --------------------------------------------------------------------------- #
# P3: attributes, mixins, hooks, external bases
# --------------------------------------------------------------------------- #


class AttributeTests(ProjectCase):
    def test_two_level_attribute_chain_with_same_module_types(self) -> None:
        self.write("app/svc.py", '''
            import time

            class Repo:
                def get(self, i: int) -> int:
                    time.sleep(1)
                    return i

            class UnitOfWork:
                def __init__(self, repo: Repo) -> None:
                    self.repo = repo

            class Service:
                def __init__(self, uow: UnitOfWork) -> None:
                    self._uow = uow

                async def load(self, i: int) -> int:
                    return self._uow.repo.get(i)
        ''')
        report = self.run_audit()
        items = [f for f in report["findings"] if f["rule"] == "ASYNC_BLOCKING_CALL_INDIRECT"]
        self.assertEqual([(f["line"], f["evidence"]) for f in items], [(17, "verified")])
        self.assertEqual(report["interprocedural"]["edges"]["attribute"], 1)
        self.assertEqual(report["interprocedural"]["uncertainty"]["dynamic_by_origin"], {})

    def test_three_level_chain_stays_dynamic_as_attribute_chain(self) -> None:
        self.write("app/svc.py", '''
            class Service:
                def __init__(self, a) -> None:
                    self.a = a

                def go(self):
                    return self.a.b.c.d()
        ''')
        stats = self.run_audit()["interprocedural"]
        self.assertEqual(stats["uncertainty"]["dynamic_by_origin"], {"attribute_chain": 1})

    def test_mixin_borrows_the_kind_of_its_unique_subclass(self) -> None:
        self.write("app/m.py", '''
            from sqlalchemy.ext.asyncio import AsyncSession
            class SessionMixin:
                async def count(self):
                    return self._session.execute("select 1")

            class ItemRepo(SessionMixin):
                def __init__(self, session: AsyncSession) -> None:
                    self._session = session
        ''')
        report = self.run_audit()
        items = [f for f in report["findings"] if f["rule"] == "SA_ASYNC_SESSION_NOT_AWAITED"]
        self.assertEqual([(f["line"], f["evidence"], f["confidence"]) for f in items], [(4, "inferred", "medium")])
        self.assertEqual(report["interprocedural"]["mixin_attribute_kinds"], 1)

    def test_conflicting_or_untyped_subclasses_cancel_the_inference(self) -> None:
        self.write("app/m.py", '''
            from sqlalchemy.ext.asyncio import AsyncSession
            from sqlalchemy.orm import Session

            class ConflictMixin:
                async def total(self):
                    return self._session.execute("select 1")

            class A(ConflictMixin):
                def __init__(self, session: AsyncSession) -> None:
                    self._session = session

            class B(ConflictMixin):
                def __init__(self, session: Session) -> None:
                    self._session = session

            class UntypedMixin:
                async def total(self):
                    return self._session.execute("select 1")

            class C(UntypedMixin):
                def __init__(self, session: AsyncSession) -> None:
                    self._session = session

            class D(UntypedMixin):
                def __init__(self, session) -> None:
                    self._session = session
        ''')
        report = self.run_audit()
        self.assertEqual(lines_of(report, "SA_ASYNC_SESSION_NOT_AWAITED"), [])
        self.assertEqual(report["interprocedural"]["mixin_attribute_kinds"], 0)

    def test_hook_with_one_implementation_is_an_inferred_edge_two_are_ambiguous(self) -> None:
        self.write("app/h.py", '''
            import time

            class SingleHookBase:
                async def run(self):
                    return self.step()

            class OnlyImpl(SingleHookBase):
                def step(self):
                    time.sleep(1)

            class HookBase:
                async def run(self):
                    return self.step()

            class HookA(HookBase):
                def step(self):
                    time.sleep(1)

            class HookB(HookBase):
                def step(self):
                    time.sleep(2)
        ''')
        report = self.run_audit()
        items = [f for f in report["findings"] if f["rule"] == "ASYNC_BLOCKING_CALL_INDIRECT"]
        self.assertEqual([(f["line"], f["evidence"], f["function"]) for f in items], [(5, "inferred", "SingleHookBase.run")])
        stats = report["interprocedural"]
        self.assertEqual(stats["subclass_hooks"], 1)
        self.assertEqual(stats["uncertainty"]["ambiguous_sites"], 1)
        self.assertEqual(stats["edges"]["interface"], 1)

    def test_external_bases_explain_unknown_methods_project_bases_do_not(self) -> None:
        self.write("app/b.py", '''
            from pydantic import BaseModel

            class Payload(BaseModel):
                name: str

                def as_json(self) -> str:
                    return self.model_dump_json()

            class Base:
                pass

            class Child(Base):
                def go(self):
                    return self.missing()
        ''')
        stats = self.run_audit()["interprocedural"]
        self.assertEqual(stats["external_base_methods"], 1)
        self.assertEqual(stats["resolution"]["not_found"], 1)


# --------------------------------------------------------------------------- #
# P4: loops
# --------------------------------------------------------------------------- #


def _classify(source: str):
    tree = ast.parse(textwrap.dedent(source))
    loops = [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.AsyncFor, ast.While))]
    outer = loops[0]
    commit = next(c for c in ast.walk(outer) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "commit")
    return classify_commit_loop(outer, commit)[0]


class LoopClassifierTests(unittest.TestCase):
    def test_per_item_shapes(self) -> None:
        self.assertEqual(_classify("for i in items:\n    s.add(i)\n    s.commit()\n"), "per_item")
        self.assertEqual(_classify("for i in items:\n    BATCH_SIZE = 10\n    s.add(i)\n    s.commit()\n"), "per_item")  # a name proves nothing
        self.assertEqual(_classify("for i in items:\n    if i % 2 == 0:\n        log(i)\n    s.commit()\n"), "per_item")  # modulo not guarding the commit
        self.assertEqual(_classify("for i in items:\n    if not i.ok:\n        break\n    s.commit()\n"), "per_item")  # early exit over a collection
        self.assertEqual(_classify("while running:\n    job = q.get()\n    s.commit()\n"), "per_item")  # no exit condition

    def test_batch_shapes(self) -> None:
        self.assertEqual(_classify("for n, i in enumerate(items):\n    s.add(i)\n    if n % 100 == 0:\n        s.commit()\n"), "batch")
        self.assertEqual(_classify("for i in items:\n    buf.append(i)\n    if len(buf) >= 50:\n        s.commit()\n        buf.clear()\n"), "batch")
        self.assertEqual(_classify("for i in items:\n    n += 1\n    if n >= 100:\n        s.commit()\n        n = 0\n"), "batch")
        self.assertEqual(_classify("for chunk in chunked(items, 100):\n    s.add_all(chunk)\n    s.commit()\n"), "batch")
        self.assertEqual(_classify("for start in range(0, total, 500):\n    s.execute(q)\n    s.commit()\n"), "batch")

    def test_paginated_and_convergence_shapes(self) -> None:
        self.assertEqual(_classify("while True:\n    rows = fetch(cursor)\n    if not rows:\n        break\n    s.commit()\n    cursor = rows[-1]\n"), "paginated")
        self.assertEqual(_classify("offset = 0\nwhile True:\n    page = fetch(offset)\n    if len(page) == 0:\n        break\n    s.commit()\n    offset += 200\n"), "paginated")
        self.assertEqual(_classify("for page in range(1000):\n    rows = fetch(page)\n    if rows is None:\n        break\n    s.commit()\n"), "paginated")
        self.assertEqual(_classify("for _ in range(3):\n    before = read()\n    s.commit()\n    after = read()\n    if before == after:\n        break\n"), "convergence")
        self.assertEqual(_classify("for _ in range(20):\n    n = prune()\n    s.commit()\n    if n < 5000:\n        break\n"), "convergence")  # short-batch exit on a counter

    def test_nested_loops_do_not_leak_their_exits(self) -> None:
        source = "for i in items:\n    for j in i.parts:\n        if not j:\n            break\n    s.commit()\n"
        self.assertEqual(_classify(source), "per_item")


class LoopRuleTests(ProjectCase):
    def test_per_item_findings_carry_details_and_keep_the_v17_fingerprint(self) -> None:
        self.write("app/m.py", '''
            from sqlalchemy.ext.asyncio import AsyncSession
            async def per_item(session: AsyncSession, items: list) -> None:
                for item in items:
                    session.add(item)
                    await session.commit()
        ''')
        item = self.findings("TX_COMMIT_IN_LOOP")[0]
        self.assertEqual(item["details"], {"loop_kind": "per_item", "loop_evidence": "no batching, pagination or convergence structure around the commit", "loop_line": 3})
        raw = "|".join(["TX_COMMIT_IN_LOOP", "app/m.py", "per_item", "session.commit", re.sub(r"\d+", "#", item["message"])])
        self.assertEqual(item["fingerprint"], hashlib.sha1(raw.encode()).hexdigest()[:16])  # details are not part of the identity
        self.assertNotIn("loop_kind", item["message"])

    def test_deliberate_kinds_are_counted_not_reported(self) -> None:
        report = audit_fixture("truth18/dev")
        self.assertEqual(report["facts"]["commit_loops"], {"batch": 2, "convergence": 1, "inside_begin": 1, "paginated": 1, "per_item": 2, "retry": 1})
        self.assertEqual(lines_of(report, "TX_COMMIT_IN_LOOP", "svc/loops.py"), [17, 42])

    def test_commit_inside_begin_block(self) -> None:
        self.write("app/m.py", '''
            from sqlalchemy.ext.asyncio import AsyncSession
            from sqlalchemy.orm import Session

            async def wrong(session: AsyncSession, items: list) -> None:
                async with session.begin():
                    for item in items:
                        session.add(item)
                        await session.commit()

            def wrong_sync(session: Session, item) -> None:
                with session.begin():
                    session.add(item)
                    session.commit()

            async def fine(session: AsyncSession, other: AsyncSession, items: list) -> None:
                async with session.begin():
                    session.add_all(items)
                    await session.flush()
                    await other.commit()

                    async def later() -> None:
                        await session.commit()
        ''')
        report = self.run_audit()
        items = [f for f in report["findings"] if f["rule"] == "TX_COMMIT_INSIDE_BEGIN_BLOCK"]
        self.assertEqual([(f["line"], f["severity"], f["confidence"], f["symbol"], f["details"]) for f in items], [(8, "high", "high", "session.commit", {"follows": True}), (13, "medium", "high", "session.commit", {"follows": False})])
        self.assertIn("InvalidRequestError", items[0]["message"])
        self.assertIn("redundant", items[1]["message"])
        self.assertEqual(lines_of(report, "TX_COMMIT_IN_LOOP"), [])  # the loop commit at line 8 is reported once, by the begin rule
        self.assertEqual(audit.RULES["TX_COMMIT_INSIDE_BEGIN_BLOCK"]["category"], "transactions")

    def test_retry_loops_stay_silent_and_counted(self) -> None:
        self.write("app/m.py", '''
            from sqlalchemy.ext.asyncio import AsyncSession
            import asyncio

            async def retry(session: AsyncSession, item) -> None:
                for attempt in range(3):
                    try:
                        session.add(item)
                        await session.commit()
                        break
                    except Exception:
                        await session.rollback()
                        await asyncio.sleep(0)
        ''')
        report = self.run_audit()
        self.assertEqual(lines_of(report, "TX_COMMIT_IN_LOOP"), [])
        self.assertEqual(report["facts"]["commit_loops"], {"retry": 1})


# --------------------------------------------------------------------------- #
# P5: remediation proposals
# --------------------------------------------------------------------------- #


class RemediationTests(ProjectCase):
    def test_selected_rules_carry_a_proposal_that_is_never_applied(self) -> None:
        report = audit_fixture("truth18/dev")
        seen = set()
        for finding in report["findings"]:
            block = finding["remediation"]
            if finding["rule"] not in REMEDIATIONS:
                self.assertIsNone(block, finding["rule"])
                continue
            seen.add(finding["rule"])
            self.assertEqual((block["status"], block["applied"], block["verified"]), ("proposed", False, False))
            self.assertIn(block["certainty"], {"high", "medium", "low"})
            for key in ("proposed_fix", "invariants", "tests"):
                self.assertTrue(block[key], (finding["rule"], key))
            self.assertIn(block["regression_risk"]["level"], {"low", "medium", "high"})
            self.assertEqual(block["evidence"]["line"], finding["line"])
            self.assertEqual(block["evidence"]["evidence_state"], finding["evidence_state"])
        self.assertEqual(seen, {"TX_COMMIT_IN_LOOP", "TX_COMMIT_INSIDE_BEGIN_BLOCK", "SA_ASYNC_SESSION_NOT_AWAITED", "ASYNC_BLOCKING_CALL_INDIRECT"})

    def test_certainty_follows_the_evidence_state(self) -> None:
        report = audit_fixture("truth18/dev")
        by_state = {}
        for f in report["findings"]:
            if f["remediation"]:
                by_state.setdefault(f["evidence_state"], set()).add(f["remediation"]["certainty"])
        self.assertEqual(by_state, {"static_confirmed": {"high"}, "inferred": {"medium"}})
        self.write("app/r.py", '''
            from fastapi import APIRouter, Depends
            from .svc import store, current_user

            router = APIRouter()

            @router.delete("/items/{item_id}")
            async def cancel(item_id: int, user=Depends(current_user)) -> dict:
                store.cancel(item_id)
                return {}
        ''')
        self.write("app/svc.py", '''
            class Store:
                def cancel(self, item_id: int) -> None:
                    pass

            store = Store()

            def current_user():
                return 1
        ''')
        items = self.findings("SEC_RESOURCE_AUTHZ_UNVERIFIED")
        self.assertEqual(len(items), 1)
        block = items[0]["remediation"]
        self.assertEqual((items[0]["evidence_state"], block["certainty"]), ("insufficient", "low"))
        self.assertTrue(block["proposed_fix"][0].startswith("Confirm the finding first"))

    def test_suppressed_findings_have_no_proposal_and_nothing_is_written(self) -> None:
        path = self.write("app/m.py", '''
            from sqlalchemy.ext.asyncio import AsyncSession
            async def per_item(session: AsyncSession, items: list) -> None:
                for item in items:
                    session.add(item)
                    await session.commit()  # audit-fastapi: ignore
        ''')
        before = {p: p.read_bytes() for p in self.root.rglob("*.py")}
        item = self.findings("TX_COMMIT_IN_LOOP")[0]
        self.assertTrue(item["suppressed"])
        self.assertIsNone(item["remediation"])
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*.py")}, before)
        self.assertEqual(sorted(p.name for p in self.root.rglob("*") if p.is_file()), [path.name])


# --------------------------------------------------------------------------- #
# P6: comparison
# --------------------------------------------------------------------------- #


BEFORE_SRC = '''
from sqlalchemy.ext.asyncio import AsyncSession
async def per_item(session: AsyncSession, items: list) -> None:
    for item in items:
        session.add(item)
        await session.commit()

async def wrong(session: AsyncSession, items: list) -> None:
    async with session.begin():
        session.add_all(items)
        await session.commit()
'''
AFTER_SRC = '''
from sqlalchemy.ext.asyncio import AsyncSession
async def per_item(session: AsyncSession, items: list) -> None:
    for item in items:
        session.add(item)
    await session.commit()

async def wrong(session: AsyncSession, items: list) -> None:
    async with session.begin():
        session.add_all(items)
        await session.commit()

async def dropped(session: AsyncSession) -> None:
    session.execute("select 1")
'''


class ComparisonTests(ProjectCase):
    def reports(self):
        self.write("app/m.py", BEFORE_SRC)
        before = self.run_audit()
        self.write("app/m.py", AFTER_SRC)
        after = self.run_audit()
        return before, after

    def test_v14_counts_are_exact_and_changes_are_additive(self) -> None:
        before, after = self.reports()
        comparison = compare_reports(after, before, "before.json")
        self.assertEqual(set(comparison["counts"]), {"new", "fixed", "unchanged"})
        self.assertEqual(comparison["counts"], {"new": 1, "fixed": 1, "unchanged": 1})
        self.assertEqual(comparison["changes"]["no_longer_detected"], 1)
        self.assertEqual(comparison["changes"]["persistent"], 1)
        self.assertEqual([i["rule"] for i in comparison["new"]], ["SA_ASYNC_SESSION_NOT_AWAITED"])
        self.assertEqual([(i["rule"], i["status"]) for i in comparison["fixed"]], [("TX_COMMIT_IN_LOOP", "no_longer_detected")])
        self.assertEqual(comparison["no_longer_detected"], comparison["fixed"])
        self.assertEqual([i["rule"] for i in comparison["persistent"]], ["TX_COMMIT_INSIDE_BEGIN_BLOCK"])
        self.assertIn("not evidence", comparison["note"])
        self.assertTrue(comparison["partial"]["comparison_reliable"])
        self.assertFalse(comparison["tool_changed"])

    def test_confidence_severity_and_evidence_changes_on_persistent_findings(self) -> None:
        before, after = self.reports()
        edited = deepcopy(before)
        target = next(f for f in edited["findings"] if f["rule"] == "TX_COMMIT_INSIDE_BEGIN_BLOCK")
        target["confidence"], target["severity"], target["evidence_state"] = "low", "low", "inferred"
        comparison = compare_reports(after, edited, "edited.json")
        self.assertEqual([(c["from"], c["to"]) for c in comparison["confidence_changes"]], [("low", "high")])
        self.assertEqual([(c["from"], c["to"]) for c in comparison["severity_changes"]], [("low", "medium")])  # commit is the block's last statement: medium
        self.assertEqual([(c["from"], c["to"]) for c in comparison["evidence_changes"]], [("inferred", "static_confirmed")])

    def test_suppression_transitions_and_partial_analysis(self) -> None:
        self.write("app/m.py", BEFORE_SRC)
        before = self.run_audit()
        self.write("app/m.py", BEFORE_SRC.replace("await session.commit()\n\nasync def wrong", "await session.commit()  # audit-fastapi: ignore\n\nasync def wrong"))
        after = self.run_audit()
        comparison = compare_reports(after, before, "b.json")
        self.assertEqual(comparison["counts"]["fixed"], 1)  # v1.4 meaning: not among the current non-suppressed findings
        self.assertEqual(comparison["changes"]["no_longer_detected"], 0)  # still detected, now suppressed
        self.assertEqual([i["rule"] for i in comparison["suppression_changes"]["newly_suppressed"]], ["TX_COMMIT_IN_LOOP"])
        partial = deepcopy(before)
        partial["summary"]["partial_analysis"] = True
        self.assertFalse(compare_reports(after, partial, "p.json")["partial"]["comparison_reliable"])
        self.assertTrue(compare_reports(after, before, "b.json")["partial"]["comparison_reliable"])

    def test_engine_baseline_option_and_cli_diff_tool(self) -> None:
        before, after = self.reports()
        (self.root / "before.json").write_text(json.dumps(before), encoding="utf-8")
        (self.root / "after.json").write_text(json.dumps(after), encoding="utf-8")
        report = self.run_audit(options=audit.AuditOptions(baseline=before, baseline_name="before.json"))
        self.assertEqual(set(report["comparison"]["counts"]), {"new", "fixed", "unchanged"})
        self.assertIn("changes", report["comparison"])
        text = self.cli("before.json", "after.json", script=DIFF_SHIM)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("no longer detected 1", text.stdout)
        self.assertNotIn("resolved", text.stdout.replace("not resolved", ""))
        gated = self.cli("before.json", "after.json", "--fail-on-new", script=DIFF_SHIM)
        self.assertEqual(gated.returncode, 1)
        as_json = self.cli("before.json", "after.json", "--format", "json", script=DIFF_SHIM)
        self.assertEqual(json.loads(as_json.stdout)["changes"]["persistent"], 1)
        markdown = self.cli("before.json", "after.json", "--format", "markdown", script=DIFF_SHIM)
        self.assertIn("### No longer detected", markdown.stdout)
        (self.root / "bad.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.cli("bad.json", "after.json", script=DIFF_SHIM).returncode, 2)


# --------------------------------------------------------------------------- #
# compatibility: schema 1.6, catalogue, determinism, security aliases
# --------------------------------------------------------------------------- #

SCHEMA_15_REPORT_KEYS = {"schema_version", "tool", "project_root", "files_scanned", "files_skipped", "findings", "summary", "facts", "interprocedural", "contracts", "comparison"}
SCHEMA_15_FINDING_KEYS = {"rule", "severity", "confidence", "file", "line", "column", "message", "symbol", "function", "environment", "suppressed", "suppression", "category", "verification", "recommended_test", "fingerprint", "evidence", "call_path", "evidence_state"}


class CompatibilityTests(ProjectCase):
    def test_schema_16_is_a_superset_of_15(self) -> None:
        report = audit_fixture("truth18/dev")
        self.assertEqual(report["schema_version"], "1.6")
        self.assertTrue(SCHEMA_15_REPORT_KEYS <= set(report))
        for finding in report["findings"]:
            self.assertTrue(SCHEMA_15_FINDING_KEYS <= set(finding))
            self.assertIn("details", finding)
            self.assertIn("remediation", finding)
        stats = report["interprocedural"]
        for key in ("project_call_coverage", "classified_ratio", "resolvable_sites", "project_site_ratio", "external_site_ratio", "unresolved_site_ratio"):
            self.assertIn(key, stats["coverage"])
        for key in ("ambiguous_sites", "dynamic_sites", "callback_params_unresolved", "interfaces", "framework_callbacks", "callback_sites_unresolved", "dynamic_by_origin"):
            self.assertIn(key, stats["uncertainty"])
        self.assertIn("commit_loops", report["facts"])

    def test_catalogue_and_list_rules(self) -> None:
        self.assertEqual(len(audit.RULES), 59)
        meta = audit.RULES["TX_COMMIT_INSIDE_BEGIN_BLOCK"]
        self.assertEqual((meta["severity"], meta["category"], meta["verification"]), ("high", "transactions", "static"))
        listed = self.cli("--list-rules")
        self.assertEqual(listed.returncode, 0)
        self.assertIn("TX_COMMIT_INSIDE_BEGIN_BLOCK", listed.stdout)

    def test_json_is_deterministic_with_the_new_blocks(self) -> None:
        first = self.cli(str(DEV), "--format", "json", "--exit-zero")
        second = self.cli(str(DEV), "--format", "json", "--exit-zero")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertIn('"remediation": {', first.stdout)
        sarif = self.cli(str(DEV), "--format", "sarif", "--exit-zero")
        self.assertEqual(sarif.returncode, 0, sarif.stderr)
        ids = {r["id"] for r in json.loads(sarif.stdout)["runs"][0]["tool"]["driver"]["rules"]}
        self.assertIn("TX_COMMIT_INSIDE_BEGIN_BLOCK", ids)

    def test_no_interprocedural_still_classifies_loops(self) -> None:
        report = audit_fixture("truth18/dev", options=audit.AuditOptions(interprocedural=False))
        self.assertEqual(lines_of(report, "TX_COMMIT_IN_LOOP", "svc/loops.py"), [17, 42])
        self.assertEqual(lines_of(report, "TX_COMMIT_INSIDE_BEGIN_BLOCK", "svc/loops.py"), [87])


class FactoryAliasTests(ProjectCase):
    FACTORY = '''
        from typing import Annotated
        from uuid import UUID
        from fastapi import Depends, FastAPI

        def authenticated() -> None:
            pass

        def admin_authenticated() -> None:
            pass

        class Store:
            def cancel(self, execution_id: UUID) -> None:
                pass

            def resolve(self, execution_id: UUID) -> None:
                pass

        def create_app(store: Store) -> FastAPI:
            app = FastAPI()
            Authenticated = Annotated[None, Depends(authenticated)]
            Admin = Annotated[None, Depends(admin_authenticated)]

            @app.delete("/v1/executions/{execution_id}")
            def cancel(execution_id: UUID, _: Authenticated) -> dict:
                store.cancel(execution_id)
                return {}

            @app.post("/v1/admin/executions/{execution_id}/resolve")
            def admin_resolve(execution_id: UUID, _: Admin) -> dict:
                store.resolve(execution_id)
                return {}

            return app
    '''

    def test_aliases_defined_in_the_factory_authenticate_and_authorize(self) -> None:
        self.write("app/service.py", self.FACTORY)
        report = self.run_audit()
        routes = {r["function"]: r for r in report["contracts"]["routes"]}
        cancel, admin = routes["create_app.cancel"], routes["create_app.admin_resolve"]
        self.assertEqual(cancel["authentication"], "present")
        self.assertTrue(cancel["authentication_evidence"][0].startswith("_: authenticated"), cancel["authentication_evidence"])
        self.assertEqual(cancel["bucket"], "authenticated_authorization_unknown")
        self.assertEqual((admin["authorization"], admin["bucket"]), (["role"], "authenticated_authorization_visible"))
        questions = [f for f in report["findings"] if f["rule"] == "SEC_RESOURCE_AUTHZ_UNVERIFIED"]
        self.assertEqual([f["function"] for f in questions], ["create_app.cancel"])
        self.assertIn("authenticated through '_: authenticated", questions[0]["message"])
        self.assertEqual(lines_of(report, "SEC_ROUTE_WITHOUT_AUTH_SIGNAL"), [])


if __name__ == "__main__":
    unittest.main()
