"""Tests for scripts/company-skill/render.py, run against a copy of a small company home.

Run: python3 -I -m unittest discover -s test/company-skill
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RENDER = REPO / "scripts" / "company-skill" / "render.py"
FIXTURE = REPO / "test" / "fixtures" / "company-skill"


class RenderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "company-example"
        shutil.copytree(FIXTURE, self.home)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def render(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(RENDER), "--root", str(self.home), "--name", "Example", "--slug", "example", *extra],
            capture_output=True,
            text=True,
        )

    def snapshot(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.home).as_posix(): path.read_bytes()
            for path in sorted(self.home.rglob("*"))
            if path.is_file()
        }

    def version(self) -> str:
        return json.loads((self.home / "plugins/example/.claude-plugin/plugin.json").read_text())["version"]

    def test_committed_fixture_rendering_is_current(self) -> None:
        result = self.render("--check")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_same_documents_give_the_same_bytes(self) -> None:
        before = self.snapshot()
        self.assertEqual(self.render().returncode, 0)
        self.assertEqual(self.snapshot(), before)

    def test_first_rendering_starts_at_0_1_0_and_adds_the_catalog_entry(self) -> None:
        shutil.rmtree(self.home / "plugins")
        catalog = self.home / ".claude-plugin/marketplace.json"
        data = json.loads(catalog.read_text())
        data["plugins"] = [p for p in data["plugins"] if p["name"] != "example"]
        catalog.write_text(json.dumps(data, indent=2) + "\n")
        result = self.render()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.version(), "0.1.0")
        entries = json.loads(catalog.read_text())["plugins"]
        self.assertEqual(entries[0]["name"], "other", "existing entries keep their place")
        self.assertEqual(entries[-1]["source"], "./plugins/example")

    def test_a_changed_document_raises_the_version_once(self) -> None:
        before = self.version()
        path = self.home / "docs/people.md"
        path.write_text(path.read_text() + "\n- Someone else joined.\n")
        self.assertEqual(self.render().returncode, 0)
        major, minor, patch = before.split(".")
        self.assertEqual(self.version(), f"{major}.{minor}.{int(patch) + 1}")
        self.assertEqual(self.render().returncode, 0)
        self.assertEqual(self.version(), f"{major}.{minor}.{int(patch) + 1}", "an unchanged rendering keeps its version")

    def test_check_names_a_stale_rendering_and_writes_nothing(self) -> None:
        path = self.home / "docs/people.md"
        path.write_text(path.read_text() + "\n- Someone else joined.\n")
        before = self.snapshot()
        result = self.render("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("AGENTS.md", result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_the_passage_names_a_location_and_no_host(self) -> None:
        for relative in ("AGENTS.md", "plugins/example/skills/example/SKILL.md"):
            text = (self.home / relative).read_text()
            self.assertIn("~/.instrukt/companies/example/", text)
            self.assertIn("`company-example`", text)
            self.assertIn("never edits what is generated", text)
            self.assertNotIn("github", text.lower())

    def test_repository_name_follows_repository_flag_when_slug_is_overridden(self) -> None:
        result = self.render("--repository", "demo/company-north")
        self.assertEqual(result.returncode, 0, result.stderr)
        for relative in ("AGENTS.md", "plugins/example/skills/example/SKILL.md"):
            text = (self.home / relative).read_text()
            self.assertIn("`company-north`", text)
            self.assertNotIn("`company-example`", text)
            self.assertNotIn("demo/", text)

    def test_a_reference_outside_the_company_is_refused(self) -> None:
        path = self.home / "docs/people.md"
        for line in (
            "See `general/policy/commits` for this.",
            "@~/.instrukt/docs/general/baseline.md",
            "Run `instrukt docs get sdlc/policy/commits`.",
            "See [this](../../elsewhere.md).",
        ):
            with self.subTest(line=line):
                original = path.read_text()
                path.write_text(original + "\n" + line + "\n")
                result = self.render("--check")
                path.write_text(original)
                self.assertEqual(result.returncode, 1)
                self.assertIn("docs/people.md", result.stderr)

    def test_a_public_document_may_not_depend_on_an_internal_one(self) -> None:
        path = self.home / "docs/products.md"
        path.write_text(path.read_text().replace("visibility: internal", "visibility: public"))
        result = self.render("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("public document may not depend", result.stderr)

    def test_an_unknown_visibility_is_refused(self) -> None:
        path = self.home / "docs/people.md"
        path.write_text(path.read_text().replace("description:", "visibility: secret\ndescription:", 1))
        result = self.render("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("visibility 'secret'", result.stderr)

    def test_whole_line_comments_are_dropped_also_over_several_lines(self) -> None:
        master = self.home / "AGENTS.master.md"
        master.write_text("<!-- one\nline two -->\n" + master.read_text())
        self.assertEqual(self.render("--check").returncode, 0)
        self.assertNotIn("<!--", (self.home / "plugins/example/skills/example/SKILL.md").read_text())

    def test_a_document_not_required_by_the_master_becomes_a_reference(self) -> None:
        skill = (self.home / "plugins/example/skills/example/SKILL.md").read_text()
        self.assertIn("`references/products.md`", skill)
        self.assertTrue((self.home / "plugins/example/skills/example/references/products.md").is_file())


if __name__ == "__main__":
    unittest.main()
