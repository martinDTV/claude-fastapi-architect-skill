#!/usr/bin/env python3
"""unittest adapter of the remediation tool (v1.9). Standard library only; copied next to the isolated copy and
run with the project's interpreter in place of ``python -m unittest ...``.

It loads the same tests ``unittest`` would (``discover -s/-t/-p`` or dotted names, ``-k`` patterns), runs them
with a result object that records every test id and its outcome, and writes a JSON report that carries the
run's nonce: discovered, selected and executed counts, passed/failed/errors/skipped ids, and loader failures
(``unittest.loader._FailedTest``: an import or attribute error) as collection errors. Exit code 0 only when
every selected test ran and passed; the report, not the exit code, is what the tool reads.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unittest
from typing import Dict, List


class RecordingResult(unittest.TestResult):
    def __init__(self) -> None:
        super().__init__()
        self.passed_ids: List[str] = []
        self.failed_ids: List[str] = []
        self.error_ids: List[str] = []
        self.skipped_ids: List[str] = []
        self._current_bad = False

    def startTest(self, test) -> None:  # noqa: N802
        super().startTest(test)
        self._current_bad = False

    def addFailure(self, test, err) -> None:  # noqa: N802
        super().addFailure(test, err)
        self.failed_ids.append(test.id())
        self._current_bad = True

    def addError(self, test, err) -> None:  # noqa: N802
        super().addError(test, err)
        self.error_ids.append(test.id())
        self._current_bad = True

    def addSkip(self, test, reason) -> None:  # noqa: N802
        super().addSkip(test, reason)
        self.skipped_ids.append(test.id())
        self._current_bad = True

    def addExpectedFailure(self, test, err) -> None:  # noqa: N802
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test) -> None:  # noqa: N802
        super().addUnexpectedSuccess(test)
        self.failed_ids.append(test.id())
        self._current_bad = True

    def stopTest(self, test) -> None:  # noqa: N802
        super().stopTest(test)
        if not self._current_bad:
            self.passed_ids.append(test.id())


def _flatten(suite) -> List[unittest.TestCase]:
    out: List[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            out.extend(_flatten(item))
        else:
            out.append(item)
    return out


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="fa_unittest_runner", add_help=False)
    parser.add_argument("--output", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("-f", "--failfast", action="store_true")
    parser.add_argument("-b", "--buffer", action="store_true")
    parser.add_argument("-c", "--catch", action="store_true")
    parser.add_argument("-k", dest="patterns", action="append", default=[])
    parser.add_argument("names", nargs="*")
    args, rest = parser.parse_known_args(argv)
    sys.path.insert(0, os.getcwd())  # what `python -m unittest` does implicitly
    loader = unittest.TestLoader()
    if args.patterns:
        loader.testNamePatterns = [p if "*" in p else f"*{p}*" for p in args.patterns]
    names = list(args.names)
    if names and names[0] == "discover":
        sub = argparse.ArgumentParser(prog="discover", add_help=False)
        sub.add_argument("-s", "--start-directory", default=".")
        sub.add_argument("-t", "--top-level-directory", default=None)
        sub.add_argument("-p", "--pattern", default="test*.py")
        sub.add_argument("-v", "--verbose", action="store_true")
        sub.add_argument("-q", "--quiet", action="store_true")
        sub_args, _ = sub.parse_known_args(rest + names[1:])
        suite = loader.discover(sub_args.start_directory, sub_args.pattern, sub_args.top_level_directory)
    elif names:
        suite = loader.loadTestsFromNames(names)
    else:
        suite = loader.discover(".")
    tests = _flatten(suite)
    collection_errors = [t.id() for t in tests if type(t).__name__ == "_FailedTest" or t.__class__.__module__ == "unittest.loader"]
    selected = [t for t in tests if t.id() not in set(collection_errors)]
    result = RecordingResult()
    unittest.TestSuite(selected).run(result)
    report: Dict[str, object] = {
        "adapter": "unittest-json",
        "nonce": args.nonce,
        "python": sys.version.split()[0],
        "argv": argv,
        # unittest semantics: an exception raised by the test (addError) is the test not passing, exactly like an
        # assertion failure; both are reported as 'failed' (pytest's 'errors' mean fixture/collection problems and
        # are infrastructure; unittest has no such separation, so nothing is reported under 'errors' here)
        "counts": {
            "discovered": len(tests),
            "selected": len(selected),
            "executed": result.testsRun,
            "passed": len(result.passed_ids),
            "failed": len(result.failed_ids) + len(result.error_ids),
            "errors": 0,
            "skipped": len(result.skipped_ids),
            "collection_errors": len(collection_errors),
        },
        "tests": {
            "passed": result.passed_ids,
            "failed": sorted(result.failed_ids + result.error_ids),
            "errors": [],
            "exceptions": result.error_ids,  # informational: which of the failed tests raised instead of asserting
            "skipped": result.skipped_ids,
            "collection_errors": collection_errors,
        },
    }
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1, sort_keys=True)
    summary = f"fa_unittest_runner: ran {result.testsRun} of {len(tests)} (passed {len(result.passed_ids)}, failed {len(result.failed_ids)}, raised {len(result.error_ids)}, skipped {len(result.skipped_ids)}, collection errors {len(collection_errors)})"
    sys.stderr.write(summary + "\n")
    ok = not result.failed_ids and not result.error_ids and not collection_errors and result.testsRun > 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
