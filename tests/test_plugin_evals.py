"""Offline validation of the plugin eval suite (plugins/fastapi-architect/evals).

Checks the case files without running `claude plugin eval` (no cost, no network):
layout, frontmatter, grader types and options, regex patterns, activation
graders on positive and negative cases, fixture code, and the absence of
personal paths or credential material.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
EVALS = REPO_ROOT / "plugins" / "fastapi-architect" / "evals"
SKILL_MD = REPO_ROOT / "plugins" / "fastapi-architect" / "skills" / "fastapi-architect" / "SKILL.md"

GRADER_TYPES = {"llm", "regex", "tool_used", "tool_order", "file_exists", "baseline"}
SKILL_MATCH = '"skill"\\s*:\\s*"(?:[\\w-]+:)?fastapi-architect"'
FORBIDDEN_CONTENT = [
    re.compile(r"/Users/[A-Za-z0-9_.-]+/"),
    re.compile(r"application_default_credentials"),
    re.compile(r"\.config/gcloud"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"ANTHROPIC_(API_KEY|FOUNDRY_API_KEY)\s*=\s*\S+"),
]


def _split_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """Minimal frontmatter reader: top-level ``key: value`` lines between --- fences."""
    assert text.startswith("---\n"), "missing frontmatter fence"
    _, fm, body = text.split("---", 2)
    fields: Dict[str, str] = {}
    for line in fm.strip("\n").splitlines():
        if not line.strip() or line.startswith(" "):
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, body


def _case_dirs() -> List[Path]:
    return sorted(
        p for p in EVALS.iterdir() if p.is_dir() and p.name != "results" and not p.name.startswith(".")
    )


def _graders(case: Path) -> List[Tuple[str, Dict[str, str], str]]:
    graders = []
    for path in sorted((case / "graders").glob("*.md")) if (case / "graders").is_dir() else []:
        fields, body = _split_frontmatter(path.read_text(encoding="utf-8"))
        graders.append((path.name, fields, body))
    case_yaml = case / "case.yaml"
    if case_yaml.is_file():
        text = case_yaml.read_text(encoding="utf-8")
        # Inline graders: "  - name: x" blocks under "graders:".
        block = text.split("graders:", 1)[1] if "graders:" in text else ""
        for chunk in re.split(r"\n\s*-\s+name:\s*", "\n" + block)[1:]:
            name = chunk.splitlines()[0].strip()
            fields = {"name": name}
            for line in chunk.splitlines()[1:]:
                m = re.match(r"\s{4}([a-z_]+):\s*(.*)$", line)
                if m:
                    fields[m.group(1)] = m.group(2).strip()
            graders.append((f"case.yaml:{name}", fields, chunk))
    return graders


def _tags(case: Path) -> List[str]:
    for name in ("prompt.md", "case.yaml"):
        path = case / name
        if path.is_file():
            m = re.search(r"^tags:\s*\[([^\]]*)\]", path.read_text(encoding="utf-8"), re.M)
            if m:
                return [t.strip() for t in m.group(1).split(",") if t.strip()]
    return []


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


class EvalSuiteLayoutTests(unittest.TestCase):
    def test_suite_exists_with_readme_and_cases(self) -> None:
        self.assertTrue(EVALS.is_dir())
        self.assertTrue((EVALS / "README.md").is_file())
        cases = _case_dirs()
        self.assertGreaterEqual(len(cases), 9)
        positives = [c for c in cases if "positive" in _tags(c)]
        negatives = [c for c in cases if "negative" in _tags(c)]
        self.assertGreaterEqual(len(positives), 6)
        self.assertGreaterEqual(len(negatives), 3)
        self.assertEqual(set(cases), set(positives) | set(negatives), "every case is tagged positive or negative")

    def test_every_case_has_a_prompt_and_valid_frontmatter(self) -> None:
        for case in _case_dirs():
            prompt_md, case_yaml = case / "prompt.md", case / "case.yaml"
            self.assertTrue(prompt_md.is_file() or case_yaml.is_file(), case.name)
            if prompt_md.is_file():
                fields, body = _split_frontmatter(prompt_md.read_text(encoding="utf-8"))
                self.assertEqual(fields.get("name"), case.name)
                self.assertTrue(body.strip(), f"{case.name}: empty prompt")
                self.assertIn("Skill", fields.get("allowed_tools", ""), f"{case.name}: Skill tool must be allowed")
                self.assertLessEqual(int(fields.get("max_turns", "10")), 20)
                self.assertLessEqual(int(fields.get("timeout_seconds", "300")), 900)
            else:
                text = case_yaml.read_text(encoding="utf-8")
                self.assertIn('schema_version: "1.1"', text)
                self.assertIn(f"name: {case.name}", text)
                self.assertIn("prompt: |", text)
                self.assertIn("Skill", text)

    def test_graders_are_well_formed(self) -> None:
        for case in _case_dirs():
            graders = _graders(case)
            self.assertTrue(graders, f"{case.name}: no graders")
            for name, fields, body in graders:
                gtype = fields.get("type")
                self.assertIn(gtype, GRADER_TYPES, f"{case.name}/{name}")
                if gtype == "regex":
                    pattern = _unquote(fields["pattern"])
                    re.compile(pattern)
                    self.assertTrue(pattern)
                if gtype == "tool_used":
                    self.assertIn("tool", fields, f"{case.name}/{name}")
                    if "input_match" in fields:
                        re.compile(_unquote(fields["input_match"]))
                if gtype == "llm":
                    text = body if not name.startswith("case.yaml") else fields.get("criteria", "") + body
                    self.assertIn("PASS", text, f"{case.name}/{name}: rubric needs PASS conditions")
                    self.assertIn("FAIL", text, f"{case.name}/{name}: rubric needs FAIL conditions")
                if "weight" in fields:
                    self.assertGreater(float(fields["weight"]), 0)


class ActivationGraderTests(unittest.TestCase):
    def _skill_graders(self, case: Path) -> List[Dict[str, str]]:
        return [
            f
            for _, f, _ in _graders(case)
            if f.get("type") == "tool_used" and f.get("tool") == "Skill" and _unquote(f.get("input_match", "")) == SKILL_MATCH
        ]

    def test_positive_cases_assert_the_skill_fires(self) -> None:
        for case in (c for c in _case_dirs() if "positive" in _tags(c)):
            graders = self._skill_graders(case)
            self.assertEqual(len(graders), 1, f"{case.name}: exactly one skill-fired grader")
            self.assertEqual(graders[0].get("arm"), "with-only", case.name)
            self.assertNotIn("max", graders[0], case.name)
            # Quality is judged as well, not only activation.
            self.assertTrue(any(f.get("type") == "llm" for _, f, _ in _graders(case)), case.name)

    def test_negative_cases_assert_the_skill_stays_quiet(self) -> None:
        for case in (c for c in _case_dirs() if "negative" in _tags(c)):
            graders = self._skill_graders(case)
            self.assertEqual(len(graders), 1, case.name)
            self.assertEqual((graders[0].get("min"), graders[0].get("max")), ("0", "0"), case.name)
            prompt = (case / "prompt.md").read_text(encoding="utf-8").lower()
            self.assertNotIn("fastapi", prompt, f"{case.name}: a negative prompt must not mention FastAPI")

    def test_skill_name_in_graders_matches_the_skill(self) -> None:
        fields, _ = _split_frontmatter(SKILL_MD.read_text(encoding="utf-8"))
        self.assertEqual(fields["name"], "fastapi-architect")
        combined = len(fields.get("description", "")) + len(fields.get("when_to_use", ""))
        self.assertLessEqual(combined, 1536, "description + when_to_use exceed the skill-listing cap")
        self.assertIn("audit", fields["description"].lower())
        self.assertIn("not for flask", fields["description"].lower())


class FixtureAndHygieneTests(unittest.TestCase):
    def test_fixture_project_parses_and_contains_the_expected_defects(self) -> None:
        fixture = EVALS / "audit-fixture-run-auditor" / "fixtures" / "sample_app"
        files = sorted(fixture.rglob("*.py"))
        self.assertGreaterEqual(len(files), 4)
        for path in files:
            ast.parse(path.read_bytes(), filename=str(path))
        main = (fixture / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("time.sleep", main)
        self.assertIn("requests.get", main)
        self.assertIn("asyncio.run(", main)
        self.assertIn("response_model=ItemRead", main)
        self.assertIn("# noqa: ASYNC221", (fixture / "tests" / "test_api.py").read_text(encoding="utf-8"))
        scaffold = EVALS / "audit-fixture-run-auditor" / "scaffold.sh"
        self.assertTrue(scaffold.is_file())
        self.assertTrue(scaffold.stat().st_mode & 0o111, "scaffold.sh must be executable")
        body = scaffold.read_text(encoding="utf-8")
        self.assertIn("fixtures/sample_app", body)
        for forbidden in ("curl ", "wget ", "pip ", "rm -rf /", "sudo "):
            self.assertNotIn(forbidden, body)
        self.assertIn("scaffold_script: scaffold.sh", (EVALS / "audit-fixture-run-auditor" / "case.yaml").read_text(encoding="utf-8"))

    def test_no_personal_paths_or_credentials_in_the_suite(self) -> None:
        for path in sorted(p for p in EVALS.rglob("*") if p.is_file() and "results" not in p.parts):
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in FORBIDDEN_CONTENT:
                self.assertIsNone(pattern.search(text), f"{path.relative_to(REPO_ROOT)}: matches {pattern.pattern}")

    def test_results_directory_is_git_ignored(self) -> None:
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("plugins/fastapi-architect/evals/results/", gitignore)

    def test_readme_documents_how_to_run(self) -> None:
        readme = (EVALS / "README.md").read_text(encoding="utf-8")
        for needle in ("claude plugin eval plugins/fastapi-architect", "--ablation none", "--allow-tools", "negative", "Activation criteria"):
            self.assertIn(needle, readme)


if __name__ == "__main__":
    unittest.main()
