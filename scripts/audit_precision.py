#!/usr/bin/env python3
"""Precision, recall, call-resolution coverage and cost of the auditor, for one or two engine versions.

Ground truth comes from inline markers in a corpus (see tests/fixtures/truth16/README.md):
``# truth: RULE``, ``# truth-absent: RULE``, ``# truth-edge: dotted.callee``,
``# truth-unresolved: STATUS``. Every finding of an evaluated rule (a rule named by any
marker) that no ``truth:`` marker accounts for is a false positive; every ``truth:`` marker
without a finding is a false negative. Markers are matched inside the enclosing function
span, so decorator-versus-def line choices do not matter.

Usage:
  audit_precision.py [--corpus DIR] [--against GIT_REV] [--extra FIXTURE_DIR ...]
                     [--root PROJECT_DIR ...] [--repeat N] [--json OUT]

``--against`` extracts the auditor of another commit with ``git archive`` into a temporary
directory and scores it on the same inputs. ``--extra`` fixtures are diffed between the two
versions (regression list). ``--root`` projects are measured only (resolution, time, memory).
Each engine runs in a subprocess so two versions never share a process. Standard library only.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_REL = "plugins/fastapi-architect/skills/fastapi-architect/scripts"
MARKER_RE = re.compile(r"#\s*truth(-absent|-edge|-unresolved)?:\s*([A-Za-z0-9_.]+)((?:\s+[a-z_]+=[A-Za-z0-9_]+)*)")
ATTR_RE = re.compile(r"([a-z_]+)=([A-Za-z0-9_]+)")


# --------------------------------------------------------------------------- #
# worker: runs inside a subprocess with one engine version on sys.path
# --------------------------------------------------------------------------- #


def collect_run(root: str) -> Dict[str, Any]:
    """Run the auditor already importable on sys.path over ``root`` and return the scorer's input
    (findings, interprocedural block, per-site resolution, time, peak RSS). In-process; used by the
    worker subprocess and by the test suite."""
    import resource

    import fastapi_audit  # noqa: E402
    from fastapi_audit.discovery import iter_python_files  # noqa: E402
    from fastapi_audit.facts import collect_facts, load_config  # noqa: E402

    start = time.monotonic()
    report = fastapi_audit.audit_project(root)
    config, src = load_config(root)
    files, _skipped = iter_python_files(root, excluded_dirs=[d for d in config.get("exclude", []) if isinstance(d, str)])
    facts = collect_facts(root, files, config=config, config_source=src)
    elapsed = time.monotonic() - start
    sites: Dict[str, List[str]] = {}
    graph = getattr(facts, "callgraph", None)
    if graph is not None:
        for (rel, line, _col), (status, callee) in graph.site_targets.items():
            sites.setdefault(f"{rel}:{line}", []).append(f"{status}|{callee or ''}")
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = rss / (1024 * 1024) if platform.system() == "Darwin" else rss / 1024
    return {
        "version": report["tool"]["version"],
        "schema": report["schema_version"],
        "findings": [
            {"rule": f["rule"], "file": f["file"], "line": f["line"], "confidence": f["confidence"], "evidence": f.get("evidence"), "details": f.get("details")}
            for f in report["findings"]
        ],
        "interprocedural": report.get("interprocedural"),
        "sites": sites,
        "elapsed_s": round(elapsed, 3),
        "max_rss_mb": round(rss_mb, 1),
        "files_scanned": report["files_scanned"],
    }


def _worker(scripts_dir: str, root: str) -> None:
    sys.dont_write_bytecode = True
    sys.path.insert(0, scripts_dir)
    sys.stdout.write(json.dumps(collect_run(root), sort_keys=True))


def run_engine(scripts_dir: str, root: str, repeat: int = 1) -> Dict[str, Any]:
    best: Optional[Dict[str, Any]] = None
    for _ in range(max(1, repeat)):
        proc = subprocess.run(
            [sys.executable, "-I", os.path.abspath(__file__), "--worker", scripts_dir, root],
            capture_output=True, text=True, timeout=1800,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"engine at {scripts_dir} failed on {root}:\n{proc.stderr[-2000:]}")
        data = json.loads(proc.stdout)
        if best is None or data["elapsed_s"] < best["elapsed_s"]:
            best = data
    assert best is not None
    return best


def extract_rev(rev: str) -> str:
    tmp = tempfile.mkdtemp(prefix="fa-engine-")
    archive = subprocess.run(["git", "-C", REPO, "archive", rev, SCRIPTS_REL], capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", tmp], input=archive.stdout, check=True)
    return os.path.join(tmp, SCRIPTS_REL)


# --------------------------------------------------------------------------- #
# truth markers
# --------------------------------------------------------------------------- #


def _function_spans(tree: ast.Module) -> List[Tuple[int, int, str]]:
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            first = min([node.lineno, *[d.lineno for d in node.decorator_list]])
            spans.append((first, getattr(node, "end_lineno", node.lineno), node.name))
    spans.sort(key=lambda s: (s[1] - s[0], s[0]))  # innermost first
    return spans


def load_truth(corpus: str) -> Dict[str, Any]:
    expected: List[Dict[str, Any]] = []
    absent: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(corpus):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__pycache__")
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, corpus).replace(os.sep, "/")
            with open(full, "r", encoding="utf-8") as handle:
                source = handle.read()
            tree = ast.parse(source)
            spans = _function_spans(tree)
            for lineno, text in enumerate(source.splitlines(), start=1):
                for m in MARKER_RE.finditer(text):
                    kind, value = m.group(1) or "", m.group(2)
                    attrs = dict(ATTR_RE.findall(m.group(3) or ""))
                    span = next(((s, e, n) for s, e, n in spans if s <= lineno <= e), (lineno, lineno, "<module>"))
                    item = {"file": rel, "line": lineno, "span": [span[0], span[1]], "function": span[2]}
                    if kind == "":
                        expected.append({**item, "rule": value, "evidence": attrs.get("evidence"), "attrs": {k: v for k, v in attrs.items() if k != "evidence"}})
                    elif kind == "-absent":
                        absent.append({**item, "rule": value})
                    elif kind == "-edge":
                        edges.append({**item, "callee": value})
                    else:
                        unresolved.append({**item, "status": value})
    return {"expected": expected, "absent": absent, "edges": edges, "unresolved": unresolved}


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #


def score(truth: Dict[str, Any], run: Dict[str, Any]) -> Dict[str, Any]:
    rules = sorted({t["rule"] for t in truth["expected"]} | {t["rule"] for t in truth["absent"]})
    findings = [f for f in run["findings"] if f["rule"] in rules]
    matched_findings: set = set()
    per_rule: Dict[str, Dict[str, int]] = {r: {"tp": 0, "fp": 0, "fn": 0, "tn": 0} for r in rules}
    fn_list, fp_list = [], []
    evidence_mismatch: List[str] = []
    for exp in truth["expected"]:
        hit = None
        for i, f in enumerate(findings):
            if i in matched_findings or f["rule"] != exp["rule"] or f["file"] != exp["file"]:
                continue
            if exp["span"][0] <= f["line"] <= exp["span"][1]:
                hit = i
                break
        if hit is None:
            per_rule[exp["rule"]]["fn"] += 1
            fn_list.append(f"{exp['rule']} {exp['file']}:{exp['line']} ({exp['function']})")
        else:
            matched_findings.add(hit)
            per_rule[exp["rule"]]["tp"] += 1
            if exp.get("evidence") and findings[hit].get("evidence") != exp["evidence"]:
                evidence_mismatch.append(f"{exp['rule']} {exp['file']}:{exp['line']} expected evidence={exp['evidence']} got {findings[hit].get('evidence')}")
            for key, value in (exp.get("attrs") or {}).items():  # e.g. loop_kind=paginated against details.loop_kind
                got = (findings[hit].get("details") or {}).get(key)
                if str(got) != value:
                    evidence_mismatch.append(f"{exp['rule']} {exp['file']}:{exp['line']} expected {key}={value} got {got}")
    for i, f in enumerate(findings):
        if i not in matched_findings:
            per_rule[f["rule"]]["fp"] += 1
            fp_list.append(f"{f['rule']} {f['file']}:{f['line']}")
    for neg in truth["absent"]:
        if not any(f["rule"] == neg["rule"] and f["file"] == neg["file"] and neg["span"][0] <= f["line"] <= neg["span"][1] for f in findings):
            per_rule[neg["rule"]]["tn"] += 1
    tp = sum(v["tp"] for v in per_rule.values())
    fp = sum(v["fp"] for v in per_rule.values())
    fn = sum(v["fn"] for v in per_rule.values())
    tn = sum(v["tn"] for v in per_rule.values())
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    fpr = fp / (fp + tn) if fp + tn else 0.0  # of the documented negatives, how many were reported
    fnr = fn / (fn + tp) if fn + tp else 0.0
    evidence_counts = {"verified": 0, "inferred": 0, "unknown": 0}
    for f in findings:
        evidence_counts[f.get("evidence") or "verified"] = evidence_counts.get(f.get("evidence") or "verified", 0) + 1
    unknown_rate = evidence_counts["unknown"] / len(findings) if findings else 0.0
    # call resolution truth
    sites = run.get("sites", {})
    edge_hits, edge_miss = 0, []
    for e in truth["edges"]:
        entries = sites.get(f"{e['file']}:{e['line']}", [])
        if any(s.startswith("project|") and s.split("|", 1)[1] == e["callee"] for s in entries):
            edge_hits += 1
        else:
            edge_miss.append(f"{e['file']}:{e['line']} -> {e['callee']} got {entries}")
    unres_hits, unres_miss = 0, []
    for u in truth["unresolved"]:
        entries = sites.get(f"{u['file']}:{u['line']}", [])
        if any(s.split("|", 1)[0] == u["status"] for s in entries) and not any(s.startswith("project|") for s in entries):
            unres_hits += 1
        else:
            unres_miss.append(f"{u['file']}:{u['line']} expected {u['status']} got {entries}")
    inter = run.get("interprocedural") or {}
    res = inter.get("resolution") or {}
    total_sites = inter.get("call_sites") or 0
    project = res.get("project", 0)
    resolvable = project + res.get("not_found", 0) + res.get("ambiguous", 0)
    return {
        "version": run["version"],
        "rules_evaluated": rules,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3),
        "false_positive_rate": round(fpr, 3), "false_negative_rate": round(fnr, 3),
        "unknown_rate": round(unknown_rate, 3), "evidence_counts": evidence_counts,
        "evidence_mismatches": evidence_mismatch,
        "per_rule": per_rule,
        "false_negatives": fn_list, "false_positives": fp_list,
        "edges": {"expected": len(truth["edges"]), "resolved": edge_hits, "missing": edge_miss},
        "unresolved": {"expected": len(truth["unresolved"]), "correct": unres_hits, "wrong": unres_miss},
        "resolution": res,
        "call_sites": total_sites,
        "project_call_coverage": round(project / resolvable, 3) if resolvable else None,
        "classified_ratio": round((total_sites - res.get("dynamic", 0)) / total_sites, 3) if total_sites else None,
        "elapsed_s": run["elapsed_s"], "max_rss_mb": run["max_rss_mb"],
        "truncated": inter.get("truncated"),
    }


def diff_findings(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, List[str]]:
    key = lambda f: f"{f['rule']} {f['file']}:{f['line']}"  # noqa: E731
    b = {key(f) for f in before["findings"]}
    a = {key(f) for f in after["findings"]}
    return {"added": sorted(a - b), "removed": sorted(b - a), "unchanged": len(a & b)}


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def render(result: Dict[str, Any]) -> str:
    lines: List[str] = []
    cols = [s for s in (result.get("before"), result.get("after")) if s]
    if cols:
        lines.append(f"## Truth corpus: {result['corpus']}")
        lines.append("")
        lines.append("| Metric | " + " | ".join(f"v{c['version']}" for c in cols) + " |")
        lines.append("|---|" + "---|" * len(cols))
        for label, k in (("True positives", "tp"), ("False positives", "fp"), ("False negatives", "fn"), ("Negatives kept silent", "tn"),
                         ("Precision", "precision"), ("Recall", "recall"), ("F1", "f1"),
                         ("False positive rate", "false_positive_rate"), ("False negative rate", "false_negative_rate"),
                         ("Unknown-evidence rate", "unknown_rate"),
                         ("Project-call coverage", "project_call_coverage"), ("Classified sites", "classified_ratio"),
                         ("Call sites", "call_sites"), ("Time (s)", "elapsed_s"), ("Peak RSS (MB)", "max_rss_mb")):
            lines.append(f"| {label} | " + " | ".join(str(c.get(k)) for c in cols) + " |")
        lines.append("| Evidence verified / inferred / unknown | " + " | ".join("/".join(str(c["evidence_counts"].get(k, 0)) for k in ("verified", "inferred", "unknown")) for c in cols) + " |")
        lines.append("| Expected edges resolved | " + " | ".join(f"{c['edges']['resolved']}/{c['edges']['expected']}" for c in cols) + " |")
        lines.append("| Expected unresolved kept | " + " | ".join(f"{c['unresolved']['correct']}/{c['unresolved']['expected']}" for c in cols) + " |")
        lines.append("")
        lines.append("| Rule | " + " | ".join(f"v{c['version']} tp/fp/fn/tn" for c in cols) + " |")
        lines.append("|---|" + "---|" * len(cols))
        for rule in cols[-1]["rules_evaluated"]:
            lines.append(f"| `{rule}` | " + " | ".join("/".join(str(c["per_rule"].get(rule, {}).get(k, 0)) for k in ("tp", "fp", "fn", "tn")) for c in cols) + " |")
        lines.append("")
        last = cols[-1]
        if last["false_negatives"] or last["false_positives"] or last["edges"]["missing"] or last["unresolved"]["wrong"] or last["evidence_mismatches"]:
            lines.append(f"Open items for v{last['version']}:")
            for item in last["false_negatives"]:
                lines.append(f"- FN {item}")
            for item in last["false_positives"]:
                lines.append(f"- FP {item}")
            for item in last["evidence_mismatches"]:
                lines.append(f"- evidence {item}")
            for item in last["edges"]["missing"]:
                lines.append(f"- edge {item}")
            for item in last["unresolved"]["wrong"]:
                lines.append(f"- unresolved {item}")
            lines.append("")
    if result.get("regressions"):
        lines.append("## Fixture diffs (before -> after)")
        lines.append("")
        for name, d in result["regressions"].items():
            lines.append(f"- `{name}`: +{len(d['added'])} / -{len(d['removed'])} / unchanged {d['unchanged']}")
            for item in d["added"]:
                lines.append(f"  - added {item}")
            for item in d["removed"]:
                lines.append(f"  - removed {item}")
        lines.append("")
    if result.get("projects"):
        lines.append("## Projects (measurement only)")
        lines.append("")
        lines.append("| Project | Version | Files | Call sites | Project edges | Coverage | Classified | Time (s) | Peak RSS (MB) | Findings |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for name, runs in result["projects"].items():
            for r in runs:
                inter = r.get("interprocedural") or {}
                res = inter.get("resolution") or {}
                project = res.get("project", 0)
                resolvable = project + res.get("not_found", 0) + res.get("ambiguous", 0)
                total = inter.get("call_sites") or 0
                cov = f"{project / resolvable:.3f}" if resolvable else "-"
                cls = f"{(total - res.get('dynamic', 0)) / total:.3f}" if total else "-"
                lines.append(f"| {name} | {r['version']} | {r['files_scanned']} | {total} | {project} | {cov} | {cls} | {r['elapsed_s']} | {r['max_rss_mb']} | {len(r['findings'])} |")
        lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv[:1] == ["--worker"]:
        _worker(argv[1], argv[2])
        return 0
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", default=os.path.join(REPO, "tests", "fixtures", "truth16"))
    parser.add_argument("--no-corpus", action="store_true", help="skip the truth corpus")
    parser.add_argument("--against", metavar="GIT_REV", help="also run the auditor from this commit")
    parser.add_argument("--extra", nargs="*", default=[], metavar="DIR", help="fixture projects to diff between versions")
    parser.add_argument("--root", nargs="*", default=[], metavar="DIR", help="projects to measure (no truth)")
    parser.add_argument("--repeat", type=int, default=1, help="runs per measurement; the fastest is kept")
    parser.add_argument("--json", metavar="FILE", help="write the full result as JSON")
    args = parser.parse_args(argv)

    current = os.path.join(REPO, SCRIPTS_REL)
    previous = extract_rev(args.against) if args.against else None
    result: Dict[str, Any] = {"corpus": os.path.relpath(args.corpus, REPO) if not args.no_corpus else None}
    if not args.no_corpus:
        truth = load_truth(args.corpus)
        result["truth_counts"] = {k: len(v) for k, v in truth.items()}
        if previous:
            result["before"] = score(truth, run_engine(previous, args.corpus, args.repeat))
        result["after"] = score(truth, run_engine(current, args.corpus, args.repeat))
    if args.extra:
        result["regressions"] = {}
        for fixture in args.extra:
            after = run_engine(current, fixture, 1)
            if previous:
                before = run_engine(previous, fixture, 1)
                result["regressions"][os.path.basename(fixture.rstrip("/"))] = diff_findings(before, after)
    if args.root:
        result["projects"] = {}
        for root in args.root:
            runs = []
            if previous:
                runs.append(run_engine(previous, root, args.repeat))
            runs.append(run_engine(current, root, args.repeat))
            for r in runs:
                r.pop("sites", None)
            result["projects"][os.path.basename(root.rstrip("/"))] = runs
    sys.stdout.write(render(result) + "\n")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
