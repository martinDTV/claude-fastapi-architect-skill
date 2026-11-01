"""v2.2 increment 0: span-exact edits on the adversarial corpus.

Every case asserts the same four things: the result parses and is the old AST plus the intended node (the primitive
proves it or refuses), every byte outside the edited span is unchanged, the comment tokens are unchanged, and a
second application is a no-op with evidence. Refusals name their reason. CRLF and tab variants are derived here.
"""

from __future__ import annotations

import ast
import sys
import tokenize
import unittest
from io import StringIO

from _audit_support import FIXTURES, SKILL_SCRIPTS

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

from fastapi_design import spans  # noqa: E402
from fastapi_design.transforms import TransformError  # noqa: E402

FIXTURE = FIXTURES / "transforms22" / "app_factory.py"
SOURCE = FIXTURE.read_text(encoding="utf-8")


def comments(source: str) -> list:
    return [t.string for t in tokenize.generate_tokens(StringIO(source).readline) if t.type == tokenize.COMMENT]


class SpanCase(unittest.TestCase):
    def assert_outside_unchanged(self, old: str, edit, extra_lines: int = 0) -> None:
        """Everything before and after the edited span is byte-identical; the new text parses; comments survive."""
        span = spans.Span(**edit.evidence["span"])
        self.assertTrue(spans.bytes_outside_unchanged(old, edit.source, span), "bytes outside the span changed")
        ast.parse(edit.source)
        self.assertEqual(comments(old), comments(edit.source))
        self.assertTrue(edit.evidence["existing_nodes_unchanged"] and edit.evidence["bytes_outside_span_unchanged"] and edit.evidence["comments_unchanged"])


class InsertStatementTests(SpanCase):
    def test_after_the_last_mount_in_a_function_body_and_idempotent(self) -> None:
        edit = spans.insert_statement("app.py", SOURCE, "app.include_router(admin)", function="create_app", after_call="app.include_router")
        self.assert_outside_unchanged(SOURCE, edit)
        lines = edit.source.splitlines()
        at = edit.evidence["inserted_at_line"]
        self.assertEqual(lines[at - 1], "    app.include_router(admin)")
        self.assertEqual(lines[at - 2], "    )")  # right after the multi-line call, the last mount
        self.assertEqual(lines[at], "    app.state.ready = True")
        again = spans.insert_statement("app.py", edit.source, "app.include_router(admin)", function="create_app", after_call="app.include_router")
        self.assertEqual((again.source, again.inserted, again.evidence["already_present"]), (edit.source, [], True))

    def test_end_of_a_method_body_and_module_level(self) -> None:
        edit = spans.insert_statement("app.py", SOURCE, "app.include_router(items)", function="Container.mount")
        self.assert_outside_unchanged(SOURCE, edit)
        at = edit.evidence["inserted_at_line"]
        self.assertEqual(edit.source.splitlines()[at - 1], "        app.include_router(items)")
        module = spans.insert_statement("app.py", SOURCE, "extra = APIRouter()")
        self.assert_outside_unchanged(SOURCE, module)
        self.assertTrue(module.source.endswith("    return app\nextra = APIRouter()\n"))
        self.assertEqual(ast.parse(module.source).body[-1].targets[0].id, "extra")  # type: ignore[attr-defined]

    def test_refusals_name_their_reason(self) -> None:
        with self.assertRaisesRegex(TransformError, "not found"):
            spans.insert_statement("app.py", SOURCE, "x = 1", function="nope")
        with self.assertRaisesRegex(TransformError, "no call to 'app.mount'"):
            spans.insert_statement("app.py", SOURCE, "x = 1", function="create_app", after_call="app.mount")
        with self.assertRaisesRegex(TransformError, "exactly one statement"):
            spans.insert_statement("app.py", SOURCE, "x = 1\ny = 2", function="create_app")
        twice = SOURCE + "\n\ndef create_app():\n    return None\n"
        with self.assertRaisesRegex(TransformError, "ambiguous"):
            spans.insert_statement("app.py", twice, "x = 1", function="create_app")
        region = spans.ProtectedRegion("app.py", 1, 200, "the operator froze this file")
        with self.assertRaisesRegex(TransformError, "protected region"):
            spans.insert_statement("app.py", SOURCE, "x = 1", function="create_app", protected=[region])
        elsewhere = spans.ProtectedRegion("other.py", 1, 200, "another file")
        spans.insert_statement("app.py", SOURCE, "x = 1", function="create_app", protected=[elsewhere])  # another path: fine

    def test_crlf_and_tab_files_keep_their_style(self) -> None:
        crlf = SOURCE.replace("\n", "\r\n")
        edit = spans.insert_statement("app.py", crlf, "app.include_router(admin)", function="create_app", after_call="app.include_router")
        self.assertNotIn("\n", edit.source.replace("\r\n", ""))
        self.assertEqual(edit.source.count("\r\n"), crlf.count("\r\n") + 1)
        self.assert_outside_unchanged(crlf, edit)
        tabs = "def make():\n\tapp = object()\n\tapp.add(1)\n\treturn app\n"
        edit = spans.insert_statement("t.py", tabs, "app.add(2)", function="make", after_call="app.add")
        self.assertEqual(edit.source, "def make():\n\tapp = object()\n\tapp.add(1)\n\tapp.add(2)\n\treturn app\n")


class InsertKeywordArgumentTests(SpanCase):
    def test_single_line_call_then_idempotent_then_refused_on_another_value(self) -> None:
        edit = spans.insert_keyword_argument("app.py", SOURCE, callee="app.include_router", function="create_app", first_argument="health", keyword="prefix", value_code='"/x"')
        self.assert_outside_unchanged(SOURCE, edit)
        self.assertIn('    app.include_router(health, prefix="/x")\n', edit.source)
        self.assertEqual(edit.evidence["style"], "inline")
        again = spans.insert_keyword_argument("app.py", edit.source, callee="app.include_router", function="create_app", first_argument="health", keyword="prefix", value_code='"/x"')
        self.assertTrue(again.evidence["already_present"])
        with self.assertRaisesRegex(TransformError, "never overwritten"):
            spans.insert_keyword_argument("app.py", edit.source, callee="app.include_router", function="create_app", first_argument="health", keyword="prefix", value_code='"/y"')

    def test_multi_line_call_with_trailing_comma_gets_its_own_line(self) -> None:
        edit = spans.insert_keyword_argument("app.py", SOURCE, callee="app.include_router", function="create_app", first_argument="items", keyword="tags", value_code='["items"]')
        self.assert_outside_unchanged(SOURCE, edit)
        self.assertIn('    app.include_router(\n        items,\n        prefix=API_PREFIX,\n        tags=["items"],\n    )\n', edit.source)
        self.assertEqual(edit.evidence["style"], "multiline")

    def test_multi_line_call_without_trailing_comma_gains_one_inside_the_span(self) -> None:
        source = "def build():\n    app = make(\n        a,\n        b\n    )\n    return app\n"
        edit = spans.insert_keyword_argument("b.py", source, callee="make", function="build", keyword="c", value_code="3")
        self.assertEqual(edit.source, "def build():\n    app = make(\n        a,\n        b,\n        c=3\n    )\n    return app\n".replace("        c=3\n", "        c=3,\n"))
        self.assertEqual(edit.evidence["style"], "multiline_added_trailing_comma")

    def test_empty_call_and_locators(self) -> None:
        source = "from fastapi import FastAPI\napp = FastAPI()\napp2 = FastAPI()\n"
        first = spans.insert_keyword_argument("m.py", source, callee="FastAPI", index=0, keyword="title", value_code='"one"')
        self.assertEqual(first.source, 'from fastapi import FastAPI\napp = FastAPI(title="one")\napp2 = FastAPI()\n')
        self.assertEqual(first.evidence["style"], "empty")
        last = spans.insert_keyword_argument("m.py", source, callee="FastAPI", keyword="title", value_code='"two"')
        self.assertEqual(last.source, 'from fastapi import FastAPI\napp = FastAPI()\napp2 = FastAPI(title="two")\n')
        with self.assertRaisesRegex(TransformError, "0 call"):
            spans.insert_keyword_argument("app.py", SOURCE, callee="app.include_router", function="create_app", first_argument="nothing", keyword="prefix", value_code='"/x"')
        with self.assertRaisesRegex(TransformError, "no call to 'app.include_router'"):
            spans.insert_keyword_argument("app.py", SOURCE, callee="app.include_router", function="plain", keyword="prefix", value_code='"/x"')

    def test_a_call_with_a_comment_or_a_kwargs_unpacking_is_refused(self) -> None:
        with self.assertRaisesRegex(TransformError, "contains a comment"):
            spans.insert_keyword_argument("app.py", SOURCE, callee="APIRouter", function="with_comment_in_call", keyword="deprecated", value_code="True")
        source = "opts = {}\nrouter = make(a, **opts)\n"
        with self.assertRaisesRegex(TransformError, "unpacks"):
            spans.insert_keyword_argument("k.py", source, callee="make", keyword="b", value_code="1")
        with self.assertRaisesRegex(TransformError, "not a valid keyword"):
            spans.insert_keyword_argument("k.py", source, callee="make", keyword="not valid", value_code="1")

    def test_crlf_call(self) -> None:
        crlf = SOURCE.replace("\n", "\r\n")
        edit = spans.insert_keyword_argument("app.py", crlf, callee="app.include_router", function="create_app", first_argument="items", keyword="tags", value_code='["items"]')
        self.assertIn('        prefix=API_PREFIX,\r\n        tags=["items"],\r\n    )\r\n', edit.source)
        self.assertNotIn("\n", edit.source.replace("\r\n", ""))


class InsertParameterTests(SpanCase):
    def test_positional_signatures(self) -> None:
        edit = spans.insert_parameter("app.py", SOURCE, function="plain", parameter_code='dep: str = "x"')
        self.assert_outside_unchanged(SOURCE, edit)
        self.assertIn('def plain(a, b=2, dep: str = "x"):\n', edit.source)
        with self.assertRaisesRegex(TransformError, "without a default cannot follow"):
            spans.insert_parameter("app.py", SOURCE, function="plain", parameter_code="dep: str")
        edit = spans.insert_parameter("app.py", SOURCE, function="create_item", parameter_code="settings2: SettingsDep")
        self.assertIn("async def create_item(payload: dict, settings: SettingsDep, settings2: SettingsDep) -> dict:\n", edit.source)
        self.assert_outside_unchanged(SOURCE, edit)

    def test_keyword_only_kwargs_and_methods(self) -> None:
        edit = spans.insert_parameter("app.py", SOURCE, function="keyword_only", parameter_code="dep: int = 1")
        self.assertIn("def keyword_only(a, *, flag: bool = False, dep: int = 1):\n", edit.source)
        self.assertTrue(edit.evidence["keyword_only"])
        edit = spans.insert_parameter("app.py", SOURCE, function="with_kwargs", parameter_code="dep: int = 1")
        self.assertIn("def with_kwargs(a, dep: int = 1, **options):\n", edit.source)
        self.assert_outside_unchanged(SOURCE, edit)
        edit = spans.insert_parameter("app.py", SOURCE, function="Container.method", parameter_code="y: int = 0")
        self.assertIn("    def method(self, x: int, y: int = 0) -> int:\n", edit.source)
        self.assert_outside_unchanged(SOURCE, edit)

    def test_multi_line_signature_with_trailing_comma(self) -> None:
        edit = spans.insert_parameter("app.py", SOURCE, function="list_items", parameter_code="principal: Annotated[str, Depends(get_settings)] = None")
        self.assert_outside_unchanged(SOURCE, edit)
        self.assertIn("    limit: Annotated[int, Query(ge=1, le=100)] = 50,\n    principal: Annotated[str, Depends(get_settings)] = None,\n) -> list[dict]:\n", edit.source)
        self.assertEqual(edit.evidence["style"], "multiline")

    def test_duplicates_comments_and_shapes_are_refused_or_noop(self) -> None:
        same = spans.insert_parameter("app.py", SOURCE, function="Container.method", parameter_code="x: int")
        self.assertTrue(same.evidence["already_present"])
        with self.assertRaisesRegex(TransformError, "already has a parameter"):
            spans.insert_parameter("app.py", SOURCE, function="Container.method", parameter_code="x: str")
        commented = "def f(a,  # the first\n      b=1):\n    return a\n"
        with self.assertRaisesRegex(TransformError, "contains a comment"):
            spans.insert_parameter("c.py", commented, function="f", parameter_code="c=2")
        with self.assertRaisesRegex(TransformError, "exactly one plain parameter"):
            spans.insert_parameter("app.py", SOURCE, function="plain", parameter_code="*args")
        with self.assertRaisesRegex(TransformError, "not a parameter"):
            spans.insert_parameter("app.py", SOURCE, function="plain", parameter_code="1 +")


class FormatterAwarenessTests(unittest.TestCase):
    def test_detection_and_command(self) -> None:
        self.assertEqual(spans.detect_formatter("[project]\nname='x'\n[tool.ruff]\nline-length = 120\n"), "ruff")
        self.assertEqual(spans.detect_formatter("[tool.ruff.format]\nquote-style = 'double'\n"), "ruff")
        self.assertEqual(spans.detect_formatter("[tool.black]\nline-length = 100\n"), "black")
        self.assertIsNone(spans.detect_formatter("[project]\nname='x'\n"))
        self.assertEqual(spans.format_check_command("ruff", ["a.py", "b.py"]), ["ruff", "format", "--check", "a.py", "b.py"])
        self.assertEqual(spans.format_check_command("black", ["a.py"]), ["black", "--check", "a.py"])
        with self.assertRaises(ValueError):
            spans.format_check_command("yapf", ["a.py"])


class HelperTests(unittest.TestCase):
    def test_spans_and_regions(self) -> None:
        tree = ast.parse(SOURCE)
        fn = spans.find_function(tree, "create_app")
        span = spans.node_span(fn)
        self.assertTrue(spans.span_text(SOURCE, span).startswith("def create_app()"))
        self.assertEqual(spans.replace_span("abc\ndef\n", spans.Span(2, 0, 2, 3), "XYZ"), "abc\nXYZ\n")
        self.assertEqual(spans.comments_in_span(SOURCE, span), [])
        self.assertTrue(spans.ProtectedRegion("p", 5, 9).touches(spans.Span(9, 0, 12, 0)))
        self.assertFalse(spans.ProtectedRegion("p", 5, 9).touches(spans.Span(10, 0, 12, 0)))
        self.assertEqual(spans.dotted_name(ast.parse("a.b.c(1)").body[0].value.func), "a.b.c")  # type: ignore[attr-defined]
        self.assertEqual(spans.newline_of("a\r\nb\r\n"), "\r\n")
        stmt, call = spans.find_call(tree, "app.include_router", function="create_app", first_argument="items")
        self.assertEqual(call.keywords[0].arg, "prefix")


if __name__ == "__main__":
    unittest.main()
