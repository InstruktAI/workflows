#!/usr/bin/env python3
"""Render a company's knowledge in its checkout and push the result when it changed.

Renders with render.py into the checkout at --root, stages exactly what the
rendering owns (AGENTS.md, plugins/<slug>/ and the catalog), and commits and
pushes it to the branch of --ref only when something changed. When the push is
refused because the branch moved, it renders again on top of the branch, at
most three times in all.

The push goes out with the credentials the checkout holds. In a workflow that
is the caller's GITHUB_TOKEN, and GitHub starts no workflow from a push made
with that token, so publishing does not loop.

Usage: publish.py --root DIR --name NAME (--slug SLUG | --repository OWNER/company-SLUG) --ref REF
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render import CATALOG, ENTRYPOINT, RenderError, manifest_version, render_into, resolve_slug  # noqa: E402 module-import-not-at-top: render is importable only after the line above puts this script's directory on sys.path, since the script runs with -I from another working directory

ATTEMPTS = 3
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


class PublishError(Exception):
    pass


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # git has no Python API in the standard library, and this script runs on a
    # bare runner with nothing installed, so it calls the git program.
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def must(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode != 0:
        raise PublishError(f"git {' '.join(result.args[1:])} failed: {result.stderr.strip()}")


def branch_of(ref: str) -> str:
    if not ref.startswith("refs/heads/"):
        raise PublishError(f"publishing needs a branch; {ref} is not one")
    return ref.removeprefix("refs/heads/")


def publish(root: Path, name: str, slug: str, ref: str) -> str:
    """Publish the rendering to the branch of ref; what was done, as one line."""
    branch = branch_of(ref)
    must(git(root, "config", "user.name", BOT_NAME))
    must(git(root, "config", "user.email", BOT_EMAIL))
    plugin_json = root / "plugins" / slug / ".claude-plugin" / "plugin.json"
    for attempt in range(1, ATTEMPTS + 1):
        render_into(root, name, slug)
        must(git(root, "add", "--all", "--", ENTRYPOINT, f"plugins/{slug}", CATALOG))
        if git(root, "diff", "--cached", "--quiet").returncode == 0:
            return f"the rendering of {slug} is unchanged; nothing to publish"
        version = manifest_version(plugin_json.read_text(encoding="utf-8"))
        must(git(root, "commit", "--quiet", "--message", f"chore: render {slug} knowledge, plugin {version}"))
        pushed = git(root, "push", "origin", f"HEAD:refs/heads/{branch}")
        if pushed.returncode == 0:
            return f"published {slug} plugin {version} to {branch}"
        print(f"push {attempt} refused; rendering again on top of {branch}: {pushed.stderr.strip()}")
        must(git(root, "fetch", "--quiet", "origin", branch))
        must(git(root, "reset", "--quiet", "--hard", "FETCH_HEAD"))
    raise PublishError(f"could not publish the rendering of {slug} after {ATTEMPTS} attempts")


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--name", required=True, help="the company's name as people write it")
    parser.add_argument("--slug", default="", help="the <slug> of the repository name company-<slug>")
    parser.add_argument("--repository", default="", help="owner/company-<slug>, read when --slug is empty")
    parser.add_argument("--ref", required=True, help="the ref the workflow runs for, such as refs/heads/main")
    args = parser.parse_args()
    try:
        print(publish(args.root.resolve(), args.name, resolve_slug(args.slug, args.repository), args.ref))
    except (RenderError, PublishError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
