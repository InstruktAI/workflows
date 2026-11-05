#!/usr/bin/env python3
"""Self-test helper: fails when a rendered comment carries active Markdown
outside code. Lines inside fenced blocks and text inside code spans are
literal; anything else may only hold the report's own fixed markup."""

import re
import sys

OWN_HTML = re.compile(r"</?(details|summary)>|<!-- dep-pr-review:[A-Za-z0-9_.-]+ -->")
ACTIVE = re.compile(r"\]\(|<|@\w|^\s*#{1,6}\s+(?!(Dependency update review|Provenance|Code review|Outcome))")

fence = None
bad = []
for number, line in enumerate(open(sys.argv[1], encoding="utf-8").read().splitlines(), 1):
    if fence:
        if line.strip() == fence:
            fence = None
        continue
    opening = re.match(r"^(`{3,})", line)
    if opening:
        fence = opening[1]
        continue
    outside = re.sub(r"(`+)(?:(?!\1).)*?\1", "", line)
    outside = OWN_HTML.sub("", outside)
    if ACTIVE.search(outside):
        bad.append(f"{number}: {line}")
if fence:
    bad.append("unclosed fence")
if bad:
    print("active markup outside code:\n" + "\n".join(bad))
    sys.exit(1)
print("comment is inert")
