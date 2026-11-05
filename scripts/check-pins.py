#!/usr/bin/env python3
"""Every `uses:` in the given workflow or action files is either local
(`./...`) or pinned to a full commit SHA with its version as a comment.

Values come from the parsed YAML (via `yq -o=json`), so block and flow style
alike are covered and a comment can never stand in for the value. The
version comment is then looked up on the line that carries that exact value.

Usage: check-pins.py FILE...   (exit 1 on any violation)
"""

import re
import sys
from pathlib import Path

PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(/[^@\s]+)?@[0-9a-f]{40}$")
DOCKER_PINNED = re.compile(r"^docker://[^@\s]+@sha256:[0-9a-f]{64}$")


sys.path.insert(0, str(Path(__file__).resolve().parent / "dep-pr-review"))
from workflow_uses import Unparseable, workflow_uses  # noqa: E402


def check(path: Path) -> list[str]:
    try:
        nodes = workflow_uses(path)
    except Unparseable as error:
        return [f"{path}: cannot parse: {error}"]
    problems = []
    for value, comment in nodes:
        if not isinstance(value, str):
            problems.append(f"{path}: `uses` is not a string: {value!r}")
            continue
        if value.startswith("./"):
            continue
        if not (PINNED.match(value) or DOCKER_PINNED.match(value)):
            problems.append(f"{path}: `{value}` is not pinned to a full commit SHA")
            continue
        if not comment:
            problems.append(f"{path}: `{value}` has no version comment")
    return problems


def main() -> int:
    problems = [p for name in sys.argv[1:] for p in check(Path(name))]
    for problem in problems:
        print(f"::error::{problem}")
    if not problems:
        print(f"{len(sys.argv) - 1} files: every `uses` is local or SHA-pinned with a version comment")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
