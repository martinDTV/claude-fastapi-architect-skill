"""Internal links of every tracked Markdown file resolve to an existing file or directory (v2.0 docs hygiene).

Relative links (``[text](path)``, ``[text](path#anchor)``) are resolved from the file's directory; absolute URLs,
mail links and bare anchors are ignored. The repository root must hold a single README and no release notes."""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
IMAGE_RE = re.compile(r"\!\[[^\]]*\]\(([^)\s]+)\)")


TEMPLATE_DIRS = ("plugins/fastapi-architect/skills/fastapi-architect/assets/",)  # templates may carry placeholder links (NNNN-title.md)


def tracked_markdown() -> list:
    """Tracked and untracked-but-not-ignored Markdown files, so new documents are checked before they are committed."""
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md", "**/*.md"], cwd=str(REPO_ROOT), capture_output=True, text=True, check=True).stdout
    return sorted({REPO_ROOT / line for line in out.splitlines() if line.strip() and not line.startswith(TEMPLATE_DIRS)})


class DocsLinkTests(unittest.TestCase):
    def test_every_relative_link_resolves(self) -> None:
        broken = []
        files = tracked_markdown()
        self.assertGreater(len(files), 30)
        for md in files:
            text = md.read_text(encoding="utf-8", errors="replace")
            for match in list(LINK_RE.finditer(text)) + list(IMAGE_RE.finditer(text)):
                target = match.group(1)
                if target.startswith(("http://", "https://", "mailto:", "#")) or "://" in target:
                    continue
                path = unquote(target.split("#", 1)[0])
                if not path:
                    continue
                resolved = (md.parent / path).resolve()
                if not resolved.exists():
                    broken.append(f"{md.relative_to(REPO_ROOT)} -> {target}")
        self.assertEqual(broken, [], "broken relative links:\n" + "\n".join(broken))

    def test_the_root_is_an_entry_point_not_an_archive(self) -> None:
        root_markdown = sorted(p.name for p in REPO_ROOT.glob("*.md"))
        self.assertEqual(root_markdown, ["README.md"])
        self.assertTrue((REPO_ROOT / "docs" / "README.md").is_file())
        releases = sorted(p.name for p in (REPO_ROOT / "docs" / "releases").glob("RELEASE-NOTES-v*.md"))
        self.assertGreaterEqual(len(releases), 8)
        index = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
        for name in releases:
            self.assertIn(f"releases/{name}", index, f"{name} is not indexed")
        self.assertIn("docs/README.md", (REPO_ROOT / "README.md").read_text(encoding="utf-8"))
