#!/usr/bin/env python3
"""Every `uses:` in the given workflow or action files is either local
(`./...`) or pinned to a full commit SHA with its version as a comment.

Values come from the parsed YAML (via `yq -o=json`), so block and flow style
alike are covered and a comment can never stand in for the value. The
version comment is then looked up on the line that carries that exact value.

Usage: check-pins.py FILE...   (exit 1 on any violation)
"""

import json
import re
import subprocess
import sys
from pathlib import Path

PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(/[^@\s]+)?@[0-9a-f]{40}$")
DOCKER_PINNED = re.compile(r"^docker://[^@\s]+@sha256:[0-9a-f]{64}$")


def uses_values(document) -> list[str]:
    """`uses` of reusable-workflow jobs, of job steps, and of composite
    action steps."""
    found = []
    if not isinstance(document, dict):
        return found
    jobs = document.get("jobs") or {}
    for job in jobs.values() if isinstance(jobs, dict) else []:
        if isinstance(job, dict):
            if "uses" in job:
                found.append(job["uses"])
            for step in job.get("steps") or []:
                if isinstance(step, dict) and "uses" in step:
                    found.append(step["uses"])
    runs = document.get("runs") or {}
    for step in (runs.get("steps") or []) if isinstance(runs, dict) else []:
        if isinstance(step, dict) and "uses" in step:
            found.append(step["uses"])
    return found


def check(path: Path) -> list[str]:
    parsed = subprocess.run(["yq", "-o=json", ".", str(path)], capture_output=True, text=True, check=False)
    if parsed.returncode != 0:
        return [f"{path}: cannot parse: {parsed.stderr.strip()}"]
    problems = []
    lines = path.read_text().splitlines()
    for value in uses_values(json.loads(parsed.stdout or "null")):
        if not isinstance(value, str):
            problems.append(f"{path}: `uses` is not a string: {value!r}")
            continue
        if value.startswith("./"):
            continue
        if not (PINNED.match(value) or DOCKER_PINNED.match(value)):
            problems.append(f"{path}: `{value}` is not pinned to a full commit SHA")
            continue
        # The comment must follow the value itself, after at most a closing
        # quote and flow-mapping punctuation.
        comment = re.compile(re.escape(value) + r"""['"]?[\s},\]]*#\s*\S""")
        if not any(comment.search(line) for line in lines):
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
