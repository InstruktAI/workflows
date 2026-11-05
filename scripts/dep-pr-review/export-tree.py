#!/usr/bin/env python3
"""Writes every tracked path of one commit to a directory, exactly as
stored, then checks the written path set equals `git ls-tree -r`.

`git archive` is not used: it honours the archived tree's .gitattributes,
so `export-ignore` drops files and `export-subst` rewrites them
(git-scm.com/docs/git-archive, ATTRIBUTES). Here paths come from
`git ls-tree -r -z --full-tree` and contents from `git cat-file --batch`,
which returns raw object contents and applies filters only when asked
with --filters or --textconv (git-scm.com/docs/git-cat-file).

Executable blobs keep their mode bit. A symlink becomes a plain file
reading `symlink -> <target>`, and a submodule pointer a file reading
`gitlink <commit>`, so a changed pointer shows up as a changed file; the
collector blocks it as an unhandled dependency.

Usage: export-tree.py REPO COMMIT DEST
"""

import os
import subprocess
import sys
from pathlib import Path

GITLINK_PREFIX = b"gitlink "


def main() -> int:
    repo, commit, dest = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    listing = subprocess.run(["git", "-C", repo, "ls-tree", "-r", "-z", "--full-tree", commit],
                             capture_output=True, check=True).stdout
    entries = []
    for record in listing.split(b"\0"):
        if not record:
            continue
        meta, path = record.split(b"\t", 1)
        mode, kind, oid = meta.split(b" ")
        entries.append((mode.decode(), kind.decode(), oid.decode(), path.decode("utf-8", "surrogateescape")))

    blobs = [e for e in entries if e[1] == "blob"]
    batch = subprocess.run(["git", "-C", repo, "cat-file", "--batch"],
                           input=b"".join(oid.encode() + b"\n" for _, _, oid, _ in blobs),
                           capture_output=True, check=True).stdout
    offset = 0
    for mode, _, oid, path in blobs:
        end = batch.index(b"\n", offset)
        header = batch[offset:end].split(b" ")
        if header[0].decode() != oid or header[1] != b"blob":
            raise SystemExit(f"unexpected cat-file header for {path}: {header!r}")
        size = int(header[2])
        content = batch[end + 1:end + 1 + size]
        offset = end + 1 + size + 1
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "120000":
            content = b"symlink -> " + content + b"\n"
        target.write_bytes(content)
        if mode == "100755":
            target.chmod(0o755)
    for mode, kind, oid, path in entries:
        if kind == "commit":
            target = dest / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(GITLINK_PREFIX + oid.encode() + b"\n")

    # Completeness: the written files are exactly the tracked paths.
    expected = {path for _, _, _, path in entries}
    written = {Path(dirpath, name).relative_to(dest).as_posix()
               for dirpath, _, names in os.walk(dest) for name in names}
    if written != expected:
        missing, extra = sorted(expected - written), sorted(written - expected)
        raise SystemExit(f"export of {commit} is incomplete: missing {missing[:10]}, extra {extra[:10]}")
    print(f"exported {len(expected)} tracked paths of {commit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
