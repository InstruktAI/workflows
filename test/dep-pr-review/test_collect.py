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


class PyPIIndex:
    """Stub PyPI: `proj` 1.0.0 and 1.1.0 with one wheel each, project_urls
    always github.com/real/proj. Attestations per version are given as
    (publisher repo, certificate repo, ref) or None."""

    def __init__(self, attestations):
        import hashlib
        self.attestations = attestations
        self.files = {v: f"wheel {v}".encode() for v in ("1.0.0", "1.1.0")}
        self.digest = {v: hashlib.sha256(d).hexdigest() for v, d in self.files.items()}

    def meta(self, version):
        name = f"proj-{version}-py3-none-any.whl"
        return {"info": {"project_urls": {"Source": "https://github.com/real/proj"}},
                "urls": [{"filename": name, "url": f"https://files.example/{name}",
                          "digests": {"sha256": self.digest[version]}, "packagetype": "bdist_wheel"}]}

    def install(self, test):
        def try_json(url, accept=None):
            if "/pypi/proj/" in url:
                return self.meta(url.split("/pypi/proj/")[1].split("/")[0])
            if "/integrity/proj/" in url:
                version = url.split("/integrity/proj/")[1].split("/")[0]
                att = self.attestations.get(version)
                if not att:
                    return None
                publisher, cert_repo, ref = att
                return {"attestation_bundles": [{"publisher": {"kind": "GitHub", "repository": publisher},
                        "attestations": [{"verification_material": {"certificate": json.dumps(
                            {"repo": cert_repo, "ref": ref, "commit": "c" * 40})}}]}]}
            return None

        def http_get(url, accept=None, limit=0):
            return self.files[url.rsplit("/", 1)[-1].split("-")[1]]

        def fulcio_claims(certificate):
            claims = json.loads(certificate)
            return {"repo": claims["repo"], "ref": claims["ref"], "commit": claims["commit"]}

        stubs = {"try_json": try_json, "http_get": http_get, "fulcio_claims": fulcio_claims,
                 "tag_commit": lambda repo, tag: "c" * 40, "release_notes": lambda repo, tags: ""}
        for name, fn in stubs.items():
            original = getattr(collect, name)
            setattr(collect, name, fn)
            test.addCleanup(setattr, collect, name, original)


def review_pypi(index):
    pin = lambda v: {"version": v, "source": "https://pypi.org/simple", "sdist": None, "wheels": [
        {"url": f"https://files.example/proj-{v}-py3-none-any.whl", "hash": f"sha256:{index.digest[v]}"}]}
    change = collect.Change("pypi", "proj", "1.0.0", "1.1.0", old_pin=pin("1.0.0"), new_pin=pin("1.1.0"))
    _, provenance, _ = collect.review_pypi(change, Path("."), Path("."))
    return provenance


class PyPIPublisherBinding(unittest.TestCase):
    def test_same_trusted_publisher_and_release_tag_passes(self):
        index = PyPIIndex({"1.0.0": ("real/proj", "real/proj", "refs/tags/v1.0.0"),
                           "1.1.0": ("real/proj", "real/proj", "refs/tags/v1.1.0")})
        index.install(self)
        self.assertEqual(review_pypi(index)["status"], "pass")

    def test_first_attestation_from_another_repository_does_not_pass(self):
        # The attack: project_urls kept, attested from the attacker's repo and tag.
        index = PyPIIndex({"1.0.0": None, "1.1.0": ("attacker/proj", "attacker/proj", "refs/tags/v1.1.0")})
        index.install(self)
        result = review_pypi(index)
        self.assertNotEqual(result["status"], "pass")
        self.assertTrue(any(c.startswith("publisher transition") for c in result["checks"]))

    def test_first_attestation_from_the_declared_repository_still_needs_a_human(self):
        index = PyPIIndex({"1.0.0": None, "1.1.0": ("real/proj", "real/proj", "refs/tags/v1.1.0")})
        index.install(self)
        self.assertEqual(review_pypi(index)["status"], "unverifiable")

    def test_certificate_repository_must_be_the_anchored_publisher(self):
        index = PyPIIndex({"1.0.0": ("real/proj", "real/proj", "refs/tags/v1.0.0"),
                           "1.1.0": ("real/proj", "attacker/proj", "refs/tags/v1.1.0")})
        index.install(self)
        result = review_pypi(index)
        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("was signed for repository attacker/proj" in c for c in result["checks"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
