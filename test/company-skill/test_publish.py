"""Tests for scripts/company-skill/publish.py against a local bare repository standing in for GitHub.

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
PUBLISH = REPO / "scripts" / "company-skill" / "publish.py"
RENDER = REPO / "scripts" / "company-skill" / "render.py"
FIXTURE = REPO / "test" / "fixtures" / "company-skill"
IDENTITY = ("-c", "user.name=Author", "-c", "user.email=author@example.com")


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *IDENTITY, *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


class PublishTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.remote = self.tmp / "remote.git"
        git(self.tmp, "init", "--quiet", "--bare", "--initial-branch=main", str(self.remote))
        author = self.tmp / "author"
        git(self.tmp, "clone", "--quiet", str(self.remote), str(author))
        shutil.copytree(FIXTURE, author, dirs_exist_ok=True)
        # The documents arrive without their rendering, as a first push would.
        shutil.rmtree(author / "plugins")
        (author / "AGENTS.md").unlink()
        catalog = author / ".claude-plugin/marketplace.json"
        data = json.loads(catalog.read_text())
        data["plugins"] = [entry for entry in data["plugins"] if entry["name"] != "example"]
        catalog.write_text(json.dumps(data, indent=2) + "\n")
        git(author, "add", "--all")
        git(author, "commit", "--quiet", "--message", "documents")
        git(author, "push", "--quiet", "origin", "HEAD:main")
        self.author = author

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def checkout(self, name: str) -> Path:
        path = self.tmp / name
        git(self.tmp, "clone", "--quiet", str(self.remote), str(path))
        return path

    def publish(self, root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(PUBLISH), "--root", str(root), "--name", "Example", *extra],
            capture_output=True,
            text=True,
        )

    def remote_log(self) -> list[str]:
        return git(self.remote, "log", "--format=%an %s", "main").splitlines()

    def test_publishes_once_then_finds_nothing_to_publish(self) -> None:
        runner = self.checkout("runner")
        first = self.publish(runner, "--repository", "owner/company-example", "--ref", "refs/heads/main")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("published example plugin 0.1.0", first.stdout)
        self.assertEqual(self.remote_log()[0], "github-actions[bot] chore: render example knowledge, plugin 0.1.0")
        rendered = subprocess.run(
            [sys.executable, "-I", str(RENDER), "--root", str(runner), "--name", "Example", "--slug", "example", "--check"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)

        again = self.checkout("again")
        second = self.publish(again, "--slug", "example", "--ref", "refs/heads/main")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("nothing to publish", second.stdout)
        self.assertEqual(len(self.remote_log()), 2)

    def test_a_branch_that_moved_is_rendered_again_on_top(self) -> None:
        stale = self.checkout("stale")
        people = self.author / "docs/people.md"
        people.write_text(people.read_text() + "\n- Someone else joined.\n")
        git(self.author, "commit", "--quiet", "--all", "--message", "a document changed")
        git(self.author, "push", "--quiet", "origin", "HEAD:main")

        result = self.publish(stale, "--slug", "example", "--ref", "refs/heads/main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("push 1 refused", result.stdout)
        log = self.remote_log()
        self.assertEqual(log[1], "Author a document changed", "the rendering sits on top of the moved branch")
        agents = git(self.remote, "show", "main:AGENTS.md")
        self.assertIn("Someone else joined.", agents)

    def test_refuses_a_ref_that_is_not_a_branch(self) -> None:
        runner = self.checkout("runner")
        result = self.publish(runner, "--slug", "example", "--ref", "refs/pull/1/merge")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not one", result.stderr)

    def test_refuses_a_repository_not_named_company_slug(self) -> None:
        runner = self.checkout("runner")
        result = self.publish(runner, "--repository", "owner/plugins", "--ref", "refs/heads/main")
        self.assertEqual(result.returncode, 1)
        self.assertIn("company-<slug>", result.stderr)


if __name__ == "__main__":
    unittest.main()
