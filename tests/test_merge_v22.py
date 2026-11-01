"""v2.2 increment 0: the pure three-way merge (text, Python proof, JSON) and its conflict and protection model."""

from __future__ import annotations

import json
import sys
import unittest

from _audit_support import SKILL_SCRIPTS

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

from fastapi_design import merge  # noqa: E402
from fastapi_design.spans import ProtectedRegion  # noqa: E402

BASE = "".join(f"line {i}\n" for i in range(1, 11))


def edited(text: str, line: int, new: str) -> str:
    lines = text.splitlines(keepends=True)
    lines[line - 1] = new + "\n"
    return "".join(lines)


def inserted(text: str, after: int, new: str) -> str:
    lines = text.splitlines(keepends=True)
    lines.insert(after, new + "\n")
    return "".join(lines)


def deleted(text: str, line: int) -> str:
    lines = text.splitlines(keepends=True)
    del lines[line - 1]
    return "".join(lines)


class TextMergeTests(unittest.TestCase):
    def test_disjoint_hunks_are_both_applied(self) -> None:
        result = merge.merge3(BASE, edited(BASE, 2, "ours two"), edited(BASE, 8, "theirs eight"))
        self.assertTrue(result.clean)
        self.assertEqual(result.text, edited(edited(BASE, 2, "ours two"), 8, "theirs eight"))
        self.assertEqual(result.applied, {"ours": 1, "theirs": 1, "identical": 0})
        self.assertEqual(result.evidence["hunks"], {"ours": 1, "theirs": 1})

    def test_two_insertions_at_one_point_conflict_unless_identical(self) -> None:
        result = merge.merge3(BASE, inserted(BASE, 5, "ours insert"), inserted(BASE, 5, "theirs insert"))
        self.assertIsNone(result.text)
        self.assertEqual([c.kind for c in result.conflicts], ["same_point"])
        self.assertEqual((result.conflicts[0].ours, result.conflicts[0].theirs), (("ours insert\n",), ("theirs insert\n",)))
        same = merge.merge3(BASE, inserted(BASE, 5, "same"), inserted(BASE, 5, "same"))
        self.assertTrue(same.clean)
        self.assertEqual(same.text, inserted(BASE, 5, "same"))

    def test_overlapping_edits_conflict_and_nothing_is_produced(self) -> None:
        ours = edited(edited(BASE, 4, "ours four"), 5, "ours five")
        theirs = edited(BASE, 5, "theirs five")
        result = merge.merge3(BASE, ours, theirs)
        self.assertIsNone(result.text)
        self.assertEqual(result.conflicts[0].kind, "overlap")
        self.assertIn("ours changes base lines 4-5", result.conflicts[0].detail)
        self.assertFalse(result.clean)

    def test_a_deletion_on_one_side_and_an_edit_elsewhere(self) -> None:
        result = merge.merge3(BASE, edited(BASE, 7, "ours seven"), deleted(BASE, 3))
        self.assertTrue(result.clean)
        self.assertEqual(result.text, deleted(edited(BASE, 7, "ours seven"), 3))

    def test_identical_changes_are_applied_once(self) -> None:
        both = edited(BASE, 4, "same four")
        result = merge.merge3(BASE, both, both)
        self.assertTrue(result.clean)
        self.assertEqual(result.text, both)
        self.assertTrue(result.evidence.get("identical_sides"))
        result = merge.merge3(BASE, edited(both, 9, "ours nine"), both)
        self.assertEqual(result.applied, {"ours": 1, "theirs": 0, "identical": 1})
        self.assertEqual(result.text, edited(both, 9, "ours nine"))

    def test_an_insertion_at_the_edge_of_a_replaced_block_is_ordered_not_conflicting(self) -> None:
        ours = edited(BASE, 3, "ours three")  # replaces line 3
        theirs = inserted(BASE, 3, "theirs after three")  # inserts after line 3 (edge of the block)
        result = merge.merge3(BASE, ours, theirs)
        self.assertTrue(result.clean, result.conflicts)
        self.assertEqual(result.text, inserted(edited(BASE, 3, "ours three"), 3, "theirs after three"))

    def test_sides_equal_to_base_or_to_each_other(self) -> None:
        ours = edited(BASE, 1, "ours one")
        self.assertEqual(merge.merge3(BASE, ours, BASE).text, ours)
        self.assertEqual(merge.merge3(BASE, BASE, ours).text, ours)
        self.assertEqual(merge.merge3(BASE, ours, ours).text, ours)
        self.assertTrue(merge.merge3(BASE, ours, BASE).evidence["theirs_unchanged"])

    def test_protected_regions_refuse_the_generated_side_only(self) -> None:
        region = ProtectedRegion("main.py", 1, 3, "operator froze the imports")
        blocked = merge.merge3(BASE, edited(BASE, 2, "ours two"), edited(BASE, 8, "theirs eight"), protected=[region], path="main.py")
        self.assertIsNone(blocked.text)
        self.assertEqual(blocked.protected_violations[0]["region"]["reason"], "operator froze the imports")
        allowed = merge.merge3(BASE, edited(BASE, 8, "ours eight"), edited(BASE, 2, "theirs two"), protected=[region], path="main.py")
        self.assertTrue(allowed.clean)
        other_file = merge.merge3(BASE, edited(BASE, 2, "ours two"), edited(BASE, 8, "theirs eight"), protected=[region], path="other.py")
        self.assertTrue(other_file.clean)

    def test_crlf_files_stay_crlf(self) -> None:
        base = BASE.replace("\n", "\r\n")
        ours = edited(BASE, 2, "ours two").replace("\n", "\r\n")
        theirs = inserted(BASE, 9, "theirs nine b").replace("\n", "\r\n")
        result = merge.merge3(base, ours, theirs)
        self.assertTrue(result.clean)
        self.assertNotIn("\n", result.text.replace("\r\n", ""))  # type: ignore[union-attr]
        self.assertIn("ours two\r\n", result.text)  # type: ignore[arg-type]
        self.assertIn("theirs nine b\r\n", result.text)  # type: ignore[arg-type]


FACTORY = "from fastapi import FastAPI\nfrom app.api import health\n\n\ndef create_app() -> FastAPI:\n    app = FastAPI()\n    app.include_router(health)\n    return app\n"


class PythonMergeTests(unittest.TestCase):
    def test_a_manual_middleware_and_a_generated_mount_merge_with_the_structural_proof(self) -> None:
        theirs = FACTORY.replace("    app = FastAPI()\n", "    app = FastAPI()\n    app.add_middleware(object)  # manual\n")
        ours = FACTORY.replace("from app.api import health\n", "from app.api import health, items\n").replace("    app.include_router(health)\n", "    app.include_router(health)\n    app.include_router(items)\n")
        result = merge.merge_python("main.py", FACTORY, ours, theirs)
        self.assertTrue(result.clean, result.conflicts)
        self.assertIn("    app.add_middleware(object)  # manual\n", result.text)  # type: ignore[arg-type]
        self.assertIn("    app.include_router(items)\n", result.text)  # type: ignore[arg-type]
        self.assertEqual(result.evidence["structural_proof"]["all_present"], True)
        self.assertNotIn("duplicate_additions", result.evidence)

    def test_both_sides_adding_the_same_import_is_one_change(self) -> None:
        both = FACTORY.replace("from fastapi import FastAPI\n", "from fastapi import FastAPI\nimport logging\n")
        result = merge.merge_python("main.py", FACTORY, both, both.replace("    return app\n", "    app.state.manual = True\n    return app\n"))
        self.assertTrue(result.clean, result.conflicts)
        self.assertEqual(result.text.count("import logging\n"), 1)  # type: ignore[union-attr]
        self.assertEqual(result.applied["identical"], 1)

    def test_the_same_mount_added_at_two_places_is_clean_but_reported_as_a_duplicate(self) -> None:
        ours = FACTORY.replace("    app.include_router(health)\n", "    app.include_router(health)\n    app.include_router(items)\n")
        theirs = FACTORY.replace("    app = FastAPI()\n", "    app = FastAPI()\n    app.include_router(items)\n")
        result = merge.merge_python("main.py", FACTORY, ours, theirs)
        self.assertTrue(result.clean, result.conflicts)
        self.assertEqual(result.text.count("app.include_router(items)"), 2)  # type: ignore[union-attr]
        # the duplicate statement sits inside the function: the top-level proof cannot see it, the copy's route
        # invariants will; what the merge reports here is that nothing was lost
        self.assertNotIn("duplicate_additions", result.evidence)
        self.assertEqual(result.evidence["structural_proof"]["all_present"], True)

    def test_a_duplicate_top_level_statement_is_reported(self) -> None:
        base = "a = 1\n"
        ours = "a = 1\nb = 2\n"
        theirs = "b = 2\na = 1\n"
        result = merge.merge_python("m.py", base, ours, theirs)
        self.assertTrue(result.clean, result.conflicts)
        self.assertEqual(result.evidence.get("duplicate_additions"), 1)

    def test_a_hand_edit_inside_a_function_the_generator_rewrites_conflicts(self) -> None:
        theirs = FACTORY.replace("    app = FastAPI()\n", "    app = FastAPI(title='mine')\n")
        ours = FACTORY.replace("    app = FastAPI()\n    app.include_router(health)\n", "    app = FastAPI(title='generated')\n    app.include_router(health, prefix='/api')\n")
        result = merge.merge_python("main.py", FACTORY, ours, theirs)
        self.assertIsNone(result.text)
        self.assertEqual(result.conflicts[0].kind, "overlap")

    def test_a_textually_clean_merge_that_does_not_parse_is_a_conflict(self) -> None:
        result = merge.merge_python("m.py", "a = 1\n", "a = (\n", "a = 1\nb = 2\n")
        self.assertIsNone(result.text)
        self.assertEqual(result.conflicts[-1].kind, "lost_node")
        self.assertIn("does not parse", result.conflicts[-1].detail)

    def test_the_presence_proof_helpers(self) -> None:
        self.assertEqual(merge._added(["a", "b"], ["a", "b", "c", "b"]), ["c", "b"])
        self.assertEqual(merge._present(["c", "b"], ["a", "b", "c"]), [])
        self.assertEqual(merge._present(["c", "b", "b"], ["a", "b", "c"]), ["b"])


class JsonMergeTests(unittest.TestCase):
    BASE = {"contracts": {"authenticated_prefixes": ["/api/v1"], "public_routes": []}, "tenant_params": ["tenant_id"], "version": 1}

    def dumps(self, obj: dict) -> str:
        return json.dumps(obj, indent=2, sort_keys=True) + "\n"

    def test_keys_changed_on_one_side_each_merge_structurally(self) -> None:
        ours = json.loads(json.dumps(self.BASE))
        ours["version"] = 2
        ours["contracts"]["public_routes"] = ["/health"]
        theirs = json.loads(json.dumps(self.BASE))
        theirs["operator_note"] = "kept"
        theirs["contracts"]["mutations_need_authorization"] = True
        result = merge.merge_json(self.dumps(self.BASE), self.dumps(ours), self.dumps(theirs))
        self.assertTrue(result.clean, result.conflicts)
        merged = json.loads(result.text)  # type: ignore[arg-type]
        self.assertEqual((merged["version"], merged["operator_note"], merged["contracts"]["public_routes"], merged["contracts"]["mutations_need_authorization"]), (2, "kept", ["/health"], True))
        self.assertEqual(result.applied, {"ours": 2, "theirs": 2, "identical": 0})

    def test_the_same_key_changed_differently_or_removed_versus_changed_conflicts(self) -> None:
        ours = dict(self.BASE, version=2)
        theirs = dict(self.BASE, version=3)
        result = merge.merge_json(self.dumps(self.BASE), self.dumps(ours), self.dumps(theirs))
        self.assertIsNone(result.text)
        self.assertIn("/version", result.conflicts[0].detail)
        removed = {k: v for k, v in self.BASE.items() if k != "tenant_params"}
        changed = dict(self.BASE, tenant_params=["org_id"])
        result = merge.merge_json(self.dumps(self.BASE), self.dumps(removed), self.dumps(changed))
        self.assertIsNone(result.text)
        self.assertIn("removed by one side and changed by the other", result.conflicts[0].detail)
        same = merge.merge_json(self.dumps(self.BASE), self.dumps(ours), self.dumps(ours))
        self.assertEqual(json.loads(same.text)["version"], 2)  # type: ignore[arg-type]
        self.assertEqual(same.applied["identical"], 1)

    def test_invalid_json_on_a_side_is_a_conflict(self) -> None:
        result = merge.merge_json(self.dumps(self.BASE), "{not json", self.dumps(self.BASE))
        self.assertIsNone(result.text)
        self.assertIn("not valid JSON", result.conflicts[0].detail)


if __name__ == "__main__":
    unittest.main()
