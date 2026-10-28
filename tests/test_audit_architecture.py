"""Architecture boundary rules (fixtures arch_modular, arch_hexagonal, arch_configured)."""

from __future__ import annotations

import unittest

from _audit_support import ProjectCase, audit_fixture, by_rule, lines_of


class ModularMonolithTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = audit_fixture("arch_modular")
        cls.grouped = by_rule(cls.report)

    def test_modules_root_is_inferred_and_reported(self) -> None:
        self.assertEqual(self.report["facts"]["modules_root"], {"path": "src/app/modules", "source": "inferred"})
        self.assertEqual(self.report["facts"]["layers"]["source"], "default")
        self.assertIsNone(self.report["facts"]["import_linter"])

    def test_cross_module_internal_import_versus_public_facade(self) -> None:
        items = sorted(self.grouped["ARCH_CROSS_MODULE_INTERNAL_IMPORT"], key=lambda f: f["file"])
        self.assertEqual([(f["file"], f["line"]) for f in items],
                         [("src/app/modules/orders/service.py", 1), ("src/app/modules/payments/service.py", 2)])
        self.assertIn("billing/public.py", items[0]["message"])
        # orders/router.py imports billing.public: allowed
        self.assertEqual(lines_of(self.report, "ARCH_CROSS_MODULE_INTERNAL_IMPORT", "src/app/modules/orders/router.py"), [])

    def test_core_must_not_import_business_modules(self) -> None:
        self.assertEqual([(f["file"], f["line"]) for f in self.grouped["ARCH_CORE_IMPORTS_MODULE"]], [("src/app/core/db.py", 1)])

    def test_repository_importing_service_and_service_importing_fastapi(self) -> None:
        self.assertEqual([f["file"] for f in self.grouped["ARCH_REPOSITORY_IMPORTS_SERVICE"]], ["src/app/modules/billing/repository.py"])
        self.assertEqual([f["file"] for f in self.grouped["ARCH_SERVICE_IMPORTS_FASTAPI"]], ["src/app/modules/billing/service.py"])
        self.assertTrue(all(f["confidence"] == "medium" for f in self.grouped["ARCH_SERVICE_IMPORTS_FASTAPI"]))  # inferred layers

    def test_domain_importing_infrastructure(self) -> None:
        items = self.grouped["ARCH_DOMAIN_IMPORTS_INFRA"]
        self.assertEqual([(f["file"], f["line"], f["symbol"]) for f in items], [("src/app/modules/payments/domain/money.py", 3, "sqlalchemy")])

    def test_import_cycles_once_per_strongly_connected_component(self) -> None:
        items = sorted(self.grouped["ARCH_IMPORT_CYCLE"], key=lambda f: f["file"])
        self.assertEqual([f["file"] for f in items], ["src/app/modules/billing/repository.py", "src/app/modules/orders/a.py"])
        self.assertIn("orders/b.py", items[1]["message"])
        self.assertEqual(items[1]["symbol"], "cycle:2")
        # function-level imports (payments/service.py) do not create graph edges

    def test_test_files_are_not_architecture(self) -> None:
        self.assertEqual([f for f in self.report["findings"] if f["file"].startswith("tests/")], [])


class HexagonalWithImportLinterTests(unittest.TestCase):
    def test_pure_hexagonal_layout_only_reports_the_import_linter_notice(self) -> None:
        report = audit_fixture("arch_hexagonal")
        self.assertEqual([f["rule"] for f in report["findings"]], ["ARCH_IMPORT_LINTER_PRESENT"])
        notice = report["findings"][0]
        self.assertEqual((notice["file"], notice["severity"]), (".importlinter", "info"))
        self.assertEqual(report["facts"]["import_linter"]["contracts"], ["Domain is pure", "Layers"])

    def test_import_linter_presence_downgrades_heuristic_findings(self) -> None:
        # same modular fixture, but with an import-linter config dropped next to it (temp copy)
        import shutil
        import tempfile
        from pathlib import Path

        from _audit_support import FIXTURES, audit

        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "proj"
            shutil.copytree(FIXTURES / "arch_modular", dst)
            (dst / "pyproject.toml").write_text(
                "[tool.importlinter]\nroot_package = \"app\"\n\n[[tool.importlinter.contracts]]\nname = \"Modules independent\"\ntype = \"independence\"\n",
                encoding="utf-8",
            )
            report = audit.audit_project(str(dst))
        grouped = by_rule(report)
        self.assertEqual(report["facts"]["import_linter"]["contracts"], ["Modules independent"])
        self.assertTrue(all(f["confidence"] == "low" for f in grouped["ARCH_CROSS_MODULE_INTERNAL_IMPORT"]))
        self.assertTrue(all(f["confidence"] == "medium" for f in grouped["ARCH_IMPORT_CYCLE"]))  # high -> medium
        self.assertIn("lint-imports", grouped["ARCH_CROSS_MODULE_INTERNAL_IMPORT"][0]["message"])


class ConfiguredBoundariesTests(unittest.TestCase):
    def test_config_file_defines_layers_and_modules_root_with_high_confidence(self) -> None:
        report = audit_fixture("arch_configured")
        self.assertEqual(report["facts"]["modules_root"], {"path": "backend/features", "source": "config"})
        self.assertEqual(report["facts"]["layers"]["source"], "config")
        self.assertEqual(report["facts"]["config_source"], ".audit-fastapi.json")
        rules = sorted((f["rule"], f["file"], f["confidence"]) for f in report["findings"])
        self.assertEqual(rules, [
            ("ARCH_CROSS_MODULE_INTERNAL_IMPORT", "backend/features/catalog/logic.py", "high"),
            ("ARCH_DOMAIN_IMPORTS_INFRA", "backend/features/pricing/model/price.py", "high"),
            ("ARCH_REPOSITORY_IMPORTS_SERVICE", "backend/features/catalog/storage.py", "high"),
            ("ARCH_SERVICE_IMPORTS_FASTAPI", "backend/features/catalog/logic.py", "high"),
        ])


class ArchitectureInlineTests(ProjectCase):
    def test_vertical_slices_without_modules_dir_produce_no_cross_module_findings(self) -> None:
        self.write("app/features/orders/handler.py", "from app.features.billing.handler import price\n")
        self.write("app/features/billing/handler.py", "def price():\n    return 1\n")
        report = self.run_audit()
        self.assertEqual(report["facts"]["modules_root"]["path"], "app/features")
        # handler.py is not an internal layer name and no public.py convention exists: still a cross-module import
        self.assertEqual([f["rule"] for f in report["findings"]], ["ARCH_CROSS_MODULE_INTERNAL_IMPORT"])

    def test_relative_imports_participate_in_cycle_detection(self) -> None:
        self.write("pkg/__init__.py", "")
        self.write("pkg/a.py", "from . import b\n")
        self.write("pkg/b.py", "from .a import A\n")
        found = self.findings("ARCH_IMPORT_CYCLE")
        self.assertEqual([(f["file"], f["symbol"]) for f in found], [("pkg/a.py", "cycle:2")])

    def test_type_checking_imports_do_not_form_cycles(self) -> None:
        self.write("pkg/__init__.py", "")
        self.write("pkg/a.py", "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from pkg.b import B\n\nclass A: ...\n")
        self.write("pkg/b.py", "from pkg.a import A\n\nclass B(A): ...\n")
        self.assertEqual(self.findings("ARCH_IMPORT_CYCLE"), [])

    def test_microservice_style_repos_without_layers_are_quiet(self) -> None:
        self.write("svc/main.py", "from fastapi import FastAPI\nfrom svc.handlers import router\napp = FastAPI()\n")
        self.write("svc/handlers.py", "from fastapi import APIRouter\nrouter = APIRouter()\n")
        self.assertEqual([r for r in self.rules() if r.startswith("ARCH_")], [])

    def test_architecture_category_can_be_disabled(self) -> None:
        from _audit_support import audit

        self.write("src/app/core/x.py", "from app.modules.a.models import M\n")
        self.write("src/app/modules/a/models.py", "M = 1\n")
        self.write("src/app/modules/b/models.py", "N = 1\n")
        self.assertIn("ARCH_CORE_IMPORTS_MODULE", self.rules())
        report = self.run_audit(options=audit.AuditOptions(categories={"sqlalchemy"}))
        self.assertEqual(report["findings"], [])


if __name__ == "__main__":
    unittest.main()
