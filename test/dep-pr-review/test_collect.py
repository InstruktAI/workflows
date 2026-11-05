"""Unit tests for collect.py decisions that no real published package
reproduces on demand. The network is stubbed; the decision code is the
real one.

Run: python3 -I test/dep-pr-review/test_collect.py
"""

import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "dep-pr-review"))
import collect  # noqa: E402

TAG_COMMIT = "a" * 40
SIBLING_REPO = "https://github.com/example/mono"


def crate_bytes(name, version, vcs):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        files = {f"{name}-{version}/Cargo.toml": f'[package]\nname = "{name}"\nversion = "{version}"\nrepository = "{SIBLING_REPO}"\n'}
        if vcs is not None:
            files[f"{name}-{version}/.cargo_vcs_info.json"] = json.dumps(vcs)
        for path, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(path)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class Registry:
    """Stub crates.io and GitHub: `mono` is tagged v1.0.0 at TAG_COMMIT, the
    companion crate `mono-core` has no tag of its own."""

    def __init__(self, core_vcs):
        self.crates = {
            ("mono", "1.0.0"): crate_bytes("mono", "1.0.0", {"git": {"sha1": TAG_COMMIT}}),
            ("mono", "0.9.0"): crate_bytes("mono", "0.9.0", {"git": {"sha1": "9" * 40}}),
            ("mono-core", "0.2.0"): crate_bytes("mono-core", "0.2.0", core_vcs),
            ("mono-core", "0.1.0"): crate_bytes("mono-core", "0.1.0", {"git": {"sha1": "8" * 40}}),
        }

    def install(self, test):
        def http_get(url, accept=None, limit=0):
            name, version = url.rsplit("/", 1)[-1][: -len(".crate")].rsplit("-", 1)
            return self.crates[(name, version)]

        def http_json(url, accept=None):
            if "/api/v1/crates/" in url:
                name, version = url.split("/api/v1/crates/")[1].split("/")[:2]
                import hashlib
                return {"version": {"checksum": hashlib.sha256(self.crates[(name, version)]).hexdigest()}}
            raise AssertionError(url)

        def tag_commit(repo, tag):
            return TAG_COMMIT if tag == "v1.0.0" else None

        def try_json(url, accept=None):
            if "/compare/" in url:
                return {"status": "ahead"}
            if "/commits/" in url:
                return {"sha": url.rsplit("/", 1)[-1]}
            return None

        for name, fn in {"http_get": http_get, "http_json": http_json, "tag_commit": tag_commit,
                         "try_json": try_json, "release_notes": lambda repo, tags: ""}.items():
            original = getattr(collect, name)
            setattr(collect, name, fn)
            test.addCleanup(setattr, collect, name, original)


def review(core_vcs):
    """Collect a PR that moves mono 0.9.0 -> 1.0.0 and, like time-core next
    to time, its untagged workspace crate mono-core 0.1.0 -> 0.2.0."""
    with tempfile.TemporaryDirectory() as tmp:
        base, head, out = Path(tmp, "base"), Path(tmp, "head"), Path(tmp, "out")
        for root, versions in ((base, {"mono": "0.9.0", "mono-core": "0.1.0"}),
                               (head, {"mono": "1.0.0", "mono-core": "0.2.0"})):
            root.mkdir()
            lock = ""
            for name, version in versions.items():
                import hashlib
                data = Registry(core_vcs).crates[(name, version)]
                lock += (f'[[package]]\nname = "{name}"\nversion = "{version}"\n'
                         f'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
                         f'checksum = "{hashlib.sha256(data).hexdigest()}"\n\n')
            (root / "Cargo.lock").write_text(lock)
        summary = collect.collect(base, head, out)
        return {d["name"]: d["provenance"] for d in summary["dependencies"]}


class CompanionSettlement(unittest.TestCase):
    def test_clean_untagged_sibling_is_settled_by_the_verified_tag(self):
        vcs = {"git": {"sha1": "b" * 40}}
        Registry(vcs).install(self)
        result = review(vcs)
        self.assertEqual(result["mono"]["status"], "pass")
        self.assertEqual(result["mono-core"]["status"], "pass")
        self.assertEqual(result["mono-core"]["companion_of"], "mono 1.0.0")

    def test_dirty_untagged_sibling_is_never_settled(self):
        vcs = {"git": {"sha1": "b" * 40, "dirty": True}}
        Registry(vcs).install(self)
        result = review(vcs)
        self.assertEqual(result["mono"]["status"], "pass")
        self.assertEqual(result["mono-core"]["status"], "unverifiable")
        self.assertIn("packaged from a dirty worktree", result["mono-core"]["checks"])
        self.assertNotIn("companion_of", result["mono-core"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
