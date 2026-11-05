"""`uses:` values of a workflow or composite-action file, from its parsed
YAML, so any key quoting and block or flow style count alike. Shared by
collect.py and scripts/check-pins.py.

Parsing goes through `yq -o=json` (mikefarah yq, preinstalled on GitHub's
hosted Ubuntu images). The version comment, which YAML parsing drops, is
read from the raw line that carries the exact parsed value.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


class Unparseable(Exception):
    pass


def uses_nodes(document) -> list:
    """`uses` of reusable-workflow jobs, of job steps, and of composite
    action steps; values of any type, as parsed."""
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


def parse(path: Path):
    parsed = subprocess.run(["yq", "-o=json", ".", str(path)], capture_output=True, text=True, check=False)
    if parsed.returncode != 0:
        raise Unparseable(parsed.stderr.strip() or "yq failed")
    return json.loads(parsed.stdout or "null")


def comment_for(text: str, value: str) -> str | None:
    """The `# ...` comment after the exact value on its line, allowing a
    closing quote and flow-mapping punctuation in between."""
    match = re.search(re.escape(value) + r"""['"]?[\s},\]]*#\s*(\S+)""", text)
    return match[1] if match else None


def workflow_uses(path: Path) -> list[tuple[object, str | None]]:
    """[(value, version comment)] for every uses node in the file."""
    text = path.read_text()
    return [(value, comment_for(text, value) if isinstance(value, str) else None)
            for value in uses_nodes(parse(path))]
