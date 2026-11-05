#!/usr/bin/env python3
"""Outcome half of dep-pr-review: decide pass/fail and render the PR comment.

Reads the collector's summary.json and the model's validated verdict.json
(either may be missing when an earlier job failed) and writes comment.md.
Exits 1 when the pull request must stay open.

The gate fails closed: a missing summary or verdict, a failed provenance
check, a dependency file this tool cannot review, nothing reviewable at all,
or a `not_sane` verdict each fail it.

Usage:
  report.py --name NAME --collect RESULT --review RESULT
            [--summary FILE] [--verdict FILE] --out FILE
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

MAX_COMMENT = 60000


def clean(text: object, limit: int = 2000) -> str:
    """Model- and upstream-written text, made inert for a PR comment: no
    mentions, no HTML, no table breaks, bounded length."""
    value = str(text)[:limit]
    value = value.replace("@", "@\u200b").replace("<", "&lt;").replace(">", "&gt;")
    value = value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", value).strip()


def load(path: Path | None):
    if not path or not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--collect", required=True, help="result of the collect job")
    parser.add_argument("--review", required=True, help="result of the review job")
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--verdict", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    summary = load(args.summary)
    verdict = load(args.verdict)
    blockers: list[str] = []
    lines = [f"<!-- dep-pr-review:{args.name} -->", "## Dependency update review", ""]

    if args.collect != "success" or summary is None:
        blockers.append("the deterministic collector did not finish; see the `collect` job log")
        summary = {"dependencies": [], "unsupported_files": []}

    deps = summary["dependencies"]
    unsupported = summary["unsupported_files"]
    if unsupported:
        blockers.append("dependency files changed that this review cannot check: "
                        + ", ".join(f"`{clean(f, 200)}`" for f in unsupported))
    if not deps and not unsupported and args.collect == "success":
        blockers.append("no updated GitHub Action, crate, PyPI or npm dependency was found to review")

    if deps:
        lines += ["### Provenance (deterministic)", "",
                  "| Dependency | Change | Result | Checks |", "| --- | --- | --- | --- |"]
        for dep in deps:
            prov = dep["provenance"]
            frm = dep.get("from_comment") or dep.get("from") or "new"
            to = dep.get("to_comment") or dep.get("to")
            if dep["ecosystem"] == "action":
                frm = f"{frm} ({(dep.get('from') or '')[:12]})"
                to = f"{to} ({dep['to'][:12]})"
            lines.append(f"| {dep['ecosystem']} `{clean(dep['name'], 200)}` | {clean(frm, 120)} → {clean(to, 120)} "
                         f"| **{prov['status']}** | {'; '.join(clean(c, 400) for c in prov['checks'])} |")
            if prov["status"] == "fail":
                blockers.append(f"provenance check failed for `{clean(dep['name'], 200)}` {clean(to, 120)}")
        flagged = [(d, d["flags"]) for d in deps if d.get("flags")]
        if flagged:
            lines += ["", "<details><summary>Pattern flags handed to the reviewer (hints, not findings)</summary>", ""]
            for dep, flags in flagged:
                lines.append(f"- `{clean(dep['name'], 200)}`: " + ", ".join(
                    f"{name} ×{info['count']}" for name, info in sorted(flags.items())))
            lines += ["", "</details>"]

    if deps:
        lines += ["", "### Code review (model)", ""]
        if args.review == "skipped":
            blockers.append("the model review did not run")
            lines.append("Not run.")
        elif args.review != "success" or verdict is None:
            blockers.append("the model review did not produce a valid verdict; see the `review` job log")
            lines.append("No valid verdict.")
        else:
            lines.append(f"Verdict: **{verdict['verdict']}**")
            lines.append("")
            for reason in verdict["reasons"]:
                lines.append(f"- {clean(reason)}")
            if verdict["findings"]:
                lines += ["", "| Dependency | Category | Severity | Detail |", "| --- | --- | --- | --- |"]
                for f in verdict["findings"]:
                    lines.append(f"| {clean(f['dependency'], 200)} | {f['category']} | {f['severity']} | {clean(f['detail'])} |")
            if verdict["verdict"] != "sane":
                blockers.append("the model review judged the code change not sane")

    lines += ["", "### Outcome", ""]
    if blockers:
        lines.append("**Blocked.** This pull request stays open for a human:")
        lines += [f"- {b}" for b in blockers]
    else:
        lines.append("**Passed.** Provenance verified and the code change matches the release notes.")
    lines += ["", "_Known limit: minified or bundled code cannot be reviewed reliably; for ordinary "
              "updates the release-age wait is the backstop._"]

    comment = "\n".join(lines)
    if len(comment) > MAX_COMMENT:
        comment = comment[:MAX_COMMENT] + "\n\n…(truncated)"
    args.out.write_text(comment + "\n")
    print(comment)
    return 1 if blockers else 0


if __name__ == "__main__":
    sys.exit(main())
