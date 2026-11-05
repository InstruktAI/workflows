#!/usr/bin/env python3
"""Deterministic half of dep-pr-review.

Compares two trees (the pull request's base and head), finds every updated
GitHub Action, crate, PyPI package and npm package, checks the provenance of
each new version against its registry and upstream repository, downloads the
published artifacts of both versions, diffs them and flags risky additions.

Everything it writes goes into one bundle directory that the model job reads.
Nothing from either tree is executed: lockfiles and workflow files are parsed
as data, artifacts are downloaded and extracted, never installed or built.

Standard library only (Python 3.11+ for tomllib), plus `diff` and `openssl`
from the runner.

Usage:
  collect.py --base DIR --head DIR --out DIR
  collect.py --scan-patch FILE      # print the flags for a unified diff
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

USER_AGENT = "InstruktAI-dep-pr-review (+https://github.com/InstruktAI/workflows)"
GITHUB_API = "https://api.github.com"
MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_PATCH_BYTES = 400 * 1024
MAX_NOTES_BYTES = 40 * 1024
SKIP_DIRS = {".git", "node_modules", "target", ".venv", "venv", "dist", "__pycache__"}

CRATES_IO_SOURCES = {
    "registry+https://github.com/rust-lang/crates.io-index",
    "sparse+https://index.crates.io/",
}
PYPI_REGISTRIES = {"https://pypi.org/simple", "https://pypi.org/simple/"}
NPM_REGISTRY = "https://registry.npmjs.org/"

# Dependency files this tool cannot review. A change to one fails closed.
UNSUPPORTED = re.compile(
    r"(^|/)(go\.sum|go\.mod|Gemfile\.lock|poetry\.lock|Pipfile\.lock|yarn\.lock|"
    r"bun\.lockb?|composer\.lock|requirements[^/]*\.txt|Dockerfile[^/]*|"
    r"\.tool-versions|\.nvmrc|\.python-version|rust-toolchain(\.toml)?)$"
)
WORKFLOW_FILE = re.compile(r"(^|/)(\.github/workflows/[^/]+|action)\.ya?ml$")
USES = re.compile(
    r"""^\s*-?\s*uses:\s*['"]?(?P<target>[^@\s'"#]+)@(?P<ref>[^\s'"#]+)['"]?"""
    r"""(?:\s*#\s*(?P<comment>\S+))?""",
    re.MULTILINE,
)
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


# --------------------------------------------------------------------------
# HTTP


class HttpError(Exception):
    def __init__(self, url: str, status: int):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status


def http_get(url: str, accept: str | None = None, limit: int = MAX_DOWNLOAD) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    token = os.environ.get("GITHUB_TOKEN")
    if token and urllib.parse.urlparse(url).netloc == "api.github.com":
        headers["Authorization"] = f"Bearer {token}"
        headers.setdefault("X-GitHub-Api-Version", "2022-11-28")
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(limit + 1)
    except urllib.error.HTTPError as error:
        raise HttpError(url, error.code) from None
    if len(data) > limit:
        raise ValueError(f"{url} is larger than {limit} bytes")
    return data


def http_json(url: str, accept: str | None = None):
    return json.loads(http_get(url, accept or "application/json"))


def try_json(url: str, accept: str | None = None):
    try:
        return http_json(url, accept)
    except HttpError as error:
        if error.status == 404:
            return None
        raise


# --------------------------------------------------------------------------
# GitHub helpers


GITHUB_URL = re.compile(r"github\.com[/:](?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?(?:[/#?].*)?$")


def github_repo(url: str | None) -> str | None:
    """owner/repo for a github.com URL in any of the usual spellings."""
    if not url:
        return None
    match = GITHUB_URL.search(url.strip())
    if not match:
        return None
    return f"{match['owner']}/{match['repo']}".lower()


def tag_commit(repo: str, tag: str) -> str | None:
    """Commit a tag points to, dereferencing annotated tags."""
    ref = try_json(f"{GITHUB_API}/repos/{repo}/git/ref/tags/{urllib.parse.quote(tag, safe='')}")
    if not ref or isinstance(ref, list):
        return None
    obj = ref["object"]
    for _ in range(5):
        if obj["type"] == "commit":
            return obj["sha"]
        if obj["type"] != "tag":
            return None
        obj = http_json(f"{GITHUB_API}/repos/{repo}/git/tags/{obj['sha']}")["object"]
    return None


def github_release(repo: str, tag: str):
    return try_json(f"{GITHUB_API}/repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}")


def commit_exists(repo: str, sha: str) -> bool:
    return try_json(f"{GITHUB_API}/repos/{repo}/commits/{sha}") is not None


def find_tag(repo: str, name: str, version: str) -> tuple[str, str] | None:
    """First release-tag spelling that exists upstream: (tag, commit)."""
    for tag in (f"v{version}", version, f"{name}-v{version}", f"{name}-{version}",
                f"{name}@{version}", f"{name}/v{version}"):
        commit = tag_commit(repo, tag)
        if commit:
            return tag, commit
    return None


def release_notes(repo: str | None, tags: list[str]) -> str:
    """Release bodies for the given tags, plus the upstream CHANGELOG at the
    newest one when the bodies are short."""
    if not repo:
        return "No upstream GitHub repository is known; no release notes.\n"
    parts = []
    for tag in tags:
        release = github_release(repo, tag)
        if release:
            parts.append(f"## {repo} release {tag}\n\n{release.get('body') or '(empty body)'}\n")
    if tags and sum(len(p) for p in parts) < 2000:
        for name in ("CHANGELOG.md", "CHANGES.md", "HISTORY.md", "RELEASES.md"):
            try:
                text = http_get(f"https://raw.githubusercontent.com/{repo}/{urllib.parse.quote(tags[-1])}/{name}",
                                limit=MAX_NOTES_BYTES * 4).decode("utf-8", "replace")
            except HttpError:
                continue
            parts.append(f"## {name} at {tags[-1]} (first {MAX_NOTES_BYTES} bytes)\n\n{text[:MAX_NOTES_BYTES]}\n")
            break
    return "\n".join(parts) or f"No GitHub release found for {', '.join(tags) or 'this version'} in {repo}.\n"


# --------------------------------------------------------------------------
# Change detection


@dataclass
class Change:
    ecosystem: str          # action | crate | pypi | npm
    name: str
    old: str | None         # old version or ref (None: newly added)
    new: str
    old_comment: str | None = None
    new_comment: str | None = None
    files: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{self.ecosystem}-{self.name}-{self.new}")
        return safe[:120]


def walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            path = Path(dirpath) / filename
            yield path.relative_to(root).as_posix(), path


def read_versions(root: Path) -> dict[tuple[str, str], dict[str, set]]:
    """{(ecosystem, name): {version_or_ref: {(comment, file), ...}}}"""
    found: dict[tuple[str, str], dict[str, set]] = {}

    def add(ecosystem, name, version, rel, comment=None):
        found.setdefault((ecosystem, name), {}).setdefault(version, set()).add((comment, rel))

    for rel, path in walk(root):
        base = path.name
        try:
            if base == "Cargo.lock":
                for package in tomllib.loads(path.read_text()).get("package", []):
                    if package.get("source") in CRATES_IO_SOURCES:
                        add("crate", package["name"], package["version"], rel)
            elif base == "uv.lock":
                for package in tomllib.loads(path.read_text()).get("package", []):
                    if (package.get("source") or {}).get("registry") in PYPI_REGISTRIES:
                        add("pypi", package["name"].lower(), package["version"], rel)
            elif base in ("package-lock.json", "npm-shrinkwrap.json"):
                data = json.loads(path.read_text())
                for key, package in (data.get("packages") or {}).items():
                    if not key or package.get("link") or "version" not in package:
                        continue
                    resolved = package.get("resolved") or ""
                    if resolved and not resolved.startswith(NPM_REGISTRY):
                        continue
                    name = package.get("name") or key.rsplit("node_modules/", 1)[-1]
                    add("npm", name, package["version"], rel)
            elif base == "pnpm-lock.yaml":
                in_packages = False
                for line in path.read_text().splitlines():
                    if re.match(r"^(packages|snapshots):\s*$", line):
                        in_packages = line.startswith("packages")
                        continue
                    if re.match(r"^\S", line):
                        in_packages = False
                    match = in_packages and re.match(r"^  '?/?((?:@[^@/\s]+/)?[^@\s'(]+)@([^\s'(:]+)", line)
                    if match:
                        add("npm", match[1], match[2], rel)
            elif WORKFLOW_FILE.search(rel):
                for match in USES.finditer(path.read_text()):
                    target = match["target"]
                    if target.startswith(("./", "docker://")):
                        continue
                    add("action", target, match["ref"], rel, match["comment"])
        except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError) as error:
            raise SystemExit(f"cannot parse {rel}: {error}")
    return found


def changed_files(base: Path, head: Path) -> list[str]:
    def digests(root):
        return {rel: hashlib.sha256(p.read_bytes()).hexdigest() for rel, p in walk(root) if p.is_file()}
    old, new = digests(base), digests(head)
    return sorted(rel for rel in set(old) | set(new) if old.get(rel) != new.get(rel))


def detect(base: Path, head: Path) -> tuple[list[Change], list[str]]:
    old_all, new_all = read_versions(base), read_versions(head)
    changes = []
    for key, new_versions in sorted(new_all.items()):
        ecosystem, name = key
        old_versions = old_all.get(key, {})
        added = sorted(set(new_versions) - set(old_versions))
        removed = sorted(set(old_versions) - set(new_versions))
        for version in added:
            old = removed[0] if len(removed) == 1 else None
            comment = sorted(c for c, _ in new_versions[version] if c) or [None]
            old_comment = sorted(c for c, _ in old_versions.get(old, set()) if c) or [None] if old else [None]
            changes.append(Change(ecosystem, name, old, version,
                                  old_comment=old_comment[0], new_comment=comment[0],
                                  files=sorted({f for _, f in new_versions[version]})))
    unsupported = [rel for rel in changed_files(base, head) if UNSUPPORTED.search(rel)]
    return changes, unsupported


# --------------------------------------------------------------------------
# Artifacts


def safe_extract_tar(data: bytes, dest: Path):
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        archive.extractall(dest, filter="data")


def safe_extract_zip(data: bytes, dest: Path):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            target = (dest / member.filename).resolve()
            if not str(target).startswith(str(dest.resolve()) + os.sep) and target != dest.resolve():
                raise ValueError(f"unsafe path in zip: {member.filename}")
        archive.extractall(dest)


def unpack(data: bytes, kind: str, dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    if kind == "zip":
        safe_extract_zip(data, dest)
    else:
        safe_extract_tar(data, dest)
    # Collapse a single top-level directory so old and new line up.
    entries = list(dest.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for child in inner.iterdir():
            shutil.move(str(child), dest / child.name)
        inner.rmdir()


def diff_trees(old: Path, new: Path) -> str:
    result = subprocess.run(["diff", "-ruN", str(old), str(new)],
                            capture_output=True, text=True, errors="replace", check=False)
    if result.returncode > 1:
        raise RuntimeError(f"diff failed: {result.stderr}")
    return result.stdout.replace(str(old) + "/", "old/").replace(str(new) + "/", "new/")


# --------------------------------------------------------------------------
# Flags: hints for the model, never a verdict on their own


FLAG_PATTERNS = {
    "network": re.compile(
        r"\b(fetch\(|XMLHttpRequest|axios|node-fetch|https?\.(request|get)\(|net\.connect|tls\.connect|"
        r"dns\.(lookup|resolve)|urllib|requests\.(get|post|put|request)|http\.client|httpx|aiohttp|"
        r"socket\.(socket|create_connection)|reqwest|hyper::|ureq::|std::net::|TcpStream|UdpSocket|"
        r"\bcurl\b|\bwget\b|Invoke-WebRequest|WebSocket)"
    ),
    "env-or-secrets": re.compile(
        r"(process\.env|os\.environ|os\.getenv|std::env::var|env::var|getenv\(|GITHUB_TOKEN|NPM_TOKEN|"
        r"ACTIONS_RUNTIME_TOKEN|ACTIONS_ID_TOKEN|AWS_[A-Z_]+|\.ssh/|id_rsa|\.npmrc|\.pypirc|"
        r"\.git-credentials|credentials|secrets\.)"
    ),
    "encoded-or-obfuscated": re.compile(
        r"([A-Za-z0-9+/]{200,}={0,2}|[0-9a-fA-F]{200,}|(\\x[0-9a-fA-F]{2}){20,}|\beval\(|new Function\(|"
        r"\batob\(|Buffer\.from\([^)]*base64|b64decode|fromCharCode|base64\s+-d|exec\(compile)"
    ),
}
INSTALL_FILES = re.compile(r"(^|/)(build\.rs|setup\.py|setup\.cfg|install\.js|postinstall[^/]*|action\.ya?ml)$")
INSTALL_LINES = re.compile(r'"(preinstall|install|postinstall|prepare|prepublish)"\s*:|^\s*(pre|post):\s|build-backend|\[build-system\]')


def scan_patch(patch: str) -> dict:
    flags: dict[str, list[str]] = {name: [] for name in FLAG_PATTERNS}
    flags["install-or-build-script"] = []
    flags["minified-or-bundled"] = []
    current = "?"
    for line in patch.splitlines():
        if line.startswith("+++ "):
            current = line[4:].split("\t")[0]
            if INSTALL_FILES.search(current):
                flags["install-or-build-script"].append(f"{current}: changed or added")
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]
        sample = f"{current}: {added.strip()[:200]}"
        for name, pattern in FLAG_PATTERNS.items():
            if pattern.search(added):
                flags[name].append(sample)
        if INSTALL_LINES.search(added):
            flags["install-or-build-script"].append(sample)
        if len(added) > 1000:
            flags["minified-or-bundled"].append(f"{current}: added line of {len(added)} characters")
    return {name: {"count": len(hits), "samples": hits[:15]} for name, hits in flags.items() if hits}


# --------------------------------------------------------------------------
# Provenance, per ecosystem. Each returns (artifacts, provenance, notes).
# artifacts: (old bytes, new bytes, kind) or None; provenance: dict with
# status pass|fail|unverifiable and the facts behind it.


def result(status: str, checks: list[str], **facts) -> dict:
    return {"status": status, "checks": checks, **facts}


def review_action(change: Change):
    parts = change.name.split("/")
    repo = "/".join(parts[:2]).lower()
    checks = []
    if not FULL_SHA.match(change.new):
        return None, result("fail", [f"`{change.name}@{change.new}` is not pinned to a full commit SHA"]), ""
    version = change.new_comment
    if not version or not re.match(r"^v?\d+(\.\d+)*", version):
        return (fetch_action(repo, change), result("fail", [
            f"no release version comment on `{change.name}@{change.new}`, so the SHA cannot be tied to a release"]),
            release_notes(repo, []))
    commit = tag_commit(repo, version)
    if commit is None:
        checks.append(f"upstream tag `{version}` does not exist in {repo}")
        status = "fail"
    elif commit != change.new:
        checks.append(f"SHA {change.new} is not the commit of upstream tag `{version}` ({commit})")
        status = "fail"
    else:
        checks.append(f"SHA is the commit of upstream tag `{version}`")
        status = "pass"
        if github_release(repo, version) is None:
            checks.append(f"no GitHub release exists for `{version}`")
            status = "fail"
        else:
            checks.append(f"GitHub release `{version}` exists")
    tags = [t for t in (change.old_comment, version) if t]
    return fetch_action(repo, change), result(status, checks, repository=repo, tag=version), release_notes(repo, tags)


def fetch_action(repo: str, change: Change):
    if not change.old or not FULL_SHA.match(change.old):
        return None
    old = http_get(f"https://codeload.github.com/{repo}/tar.gz/{change.old}")
    new = http_get(f"https://codeload.github.com/{repo}/tar.gz/{change.new}")
    return old, new, "tar"


def crate_files(name: str, version: str) -> tuple[bytes, dict]:
    data = http_get(f"https://static.crates.io/crates/{name}/{name}-{version}.crate")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        def member(suffix):
            for m in archive.getmembers():
                if m.name.count("/") == 1 and m.name.endswith("/" + suffix):
                    handle = archive.extractfile(m)
                    return handle.read().decode("utf-8", "replace") if handle else None
            return None
        vcs = member(".cargo_vcs_info.json")
        manifest = member("Cargo.toml") or ""
    repository = None
    try:
        repository = tomllib.loads(manifest).get("package", {}).get("repository")
    except tomllib.TOMLDecodeError:
        pass
    return data, {"vcs": json.loads(vcs) if vcs else None, "repository": repository,
                  "sha256": hashlib.sha256(data).hexdigest()}


def review_crate(change: Change, base: Path, head: Path):
    checks = []
    status = "pass"
    meta = http_json(f"https://crates.io/api/v1/crates/{change.name}/{change.new}")["version"]
    if meta.get("yanked"):
        checks.append("the new version is yanked on crates.io")
        status = "fail"
    new_data, new = crate_files(change.name, change.new)
    if meta.get("checksum") and meta["checksum"] != new["sha256"]:
        checks.append("downloaded .crate does not match the checksum crates.io lists")
        status = "fail"
    old_data, old = (crate_files(change.name, change.old) if change.old else (None, None))
    expected = old["repository"] if old else http_json(f"https://crates.io/api/v1/crates/{change.name}")["crate"].get("repository")
    repo = github_repo(new["repository"])
    if github_repo(expected) != repo:
        checks.append(f"repository changed: `{expected}` -> `{new['repository']}`")
        status = "fail"
    else:
        checks.append(f"repository `{new['repository']}` matches the previous version")
    vcs = new["vcs"]
    sha = (vcs or {}).get("git", {}).get("sha1")
    tag = None
    if not sha:
        checks.append("the .crate has no .cargo_vcs_info.json commit; the source cannot be tied to a commit")
        status = "fail" if status == "fail" else "unverifiable"
    elif not repo:
        checks.append(f"repository `{new['repository']}` is not on GitHub; commit {sha} not checked")
        status = "fail" if status == "fail" else "unverifiable"
    else:
        if vcs.get("git", {}).get("dirty"):
            checks.append("packaged from a dirty worktree")
            status = "fail" if status == "fail" else "unverifiable"
        found = find_tag(repo, change.name, change.new)
        if found and found[1] == sha:
            tag = found[0]
            checks.append(f".cargo_vcs_info.json commit {sha} is upstream tag `{tag}`")
        elif found:
            checks.append(f".cargo_vcs_info.json commit {sha} is not upstream tag `{found[0]}` ({found[1]})")
            status = "fail"
        elif commit_exists(repo, sha):
            checks.append(f"commit {sha} exists upstream but no release tag for {change.new} was found")
            status = "fail" if status == "fail" else "unverifiable"
        else:
            checks.append(f"commit {sha} does not exist in {repo}")
            status = "fail"
    artifacts = (old_data, new_data, "tar") if old_data else None
    tags = [t for t in (tag,) if t]
    return artifacts, result(status, checks, repository=new["repository"], commit=sha, tag=tag), release_notes(repo, tags)


def npm_packument(name: str, version: str):
    quoted = name.replace("/", "%2f")
    return http_json(f"{NPM_REGISTRY}{quoted}/{version}")


def npm_keys() -> dict:
    return {k["keyid"]: k for k in http_json(f"{NPM_REGISTRY}-/npm/v1/keys")["keys"]}


def verify_npm_signature(name: str, version: str, dist: dict, keys: dict) -> str | None:
    """None when a registry signature verifies, else the reason it does not."""
    for signature in dist.get("signatures") or []:
        key = keys.get(signature.get("keyid"))
        if not key:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            pem = Path(tmp, "key.pem")
            pem.write_text("-----BEGIN PUBLIC KEY-----\n" + key["key"] + "\n-----END PUBLIC KEY-----\n")
            Path(tmp, "sig").write_bytes(base64.b64decode(signature["sig"]))
            Path(tmp, "msg").write_text(f"{name}@{version}:{dist['integrity']}")
            ok = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pem), "-signature",
                                 str(Path(tmp, "sig")), str(Path(tmp, "msg"))],
                                capture_output=True, check=False).returncode == 0
        if ok:
            return None
        return f"registry signature with key {signature['keyid']} does not verify"
    return "no registry signature with a known npm signing key"


def npm_provenance(meta: dict, integrity_hex: str) -> tuple[str | None, str | None]:
    """(source repository, problem) from the SLSA provenance attestation."""
    attestations = (meta.get("dist") or {}).get("attestations")
    if not attestations:
        return None, None
    bundle = http_json(attestations["url"])
    for attestation in bundle.get("attestations", []):
        if "slsa.dev/provenance" not in attestation.get("predicateType", ""):
            continue
        statement = json.loads(base64.b64decode(attestation["bundle"]["dsseEnvelope"]["payload"]))
        digests = [s.get("digest", {}).get("sha512") for s in statement.get("subject", [])]
        if integrity_hex not in digests:
            return None, "provenance subject digest does not match the published tarball"
        workflow = statement["predicate"]["buildDefinition"]["externalParameters"]["workflow"]
        return github_repo(workflow.get("repository")), None
    return None, "attestations listed but no SLSA provenance among them"


def review_npm(change: Change, base: Path, head: Path):
    checks = []
    status = "pass"
    keys = npm_keys()
    meta = npm_packument(change.name, change.new)
    dist = meta["dist"]
    new_data = http_get(dist["tarball"])
    integrity = dist["integrity"]
    algo, _, digest_b64 = integrity.partition("-")
    integrity_hex = base64.b64decode(digest_b64).hex()
    if hashlib.new(algo, new_data).hexdigest() != integrity_hex:
        checks.append("downloaded tarball does not match the registry integrity")
        status = "fail"
    problem = verify_npm_signature(change.name, change.new, dist, keys)
    if problem:
        checks.append(problem)
        status = "fail"
    else:
        checks.append("npm registry signature verifies (ECDSA P-256 over name@version:integrity)")
    declared = github_repo((meta.get("repository") or {}).get("url") if isinstance(meta.get("repository"), dict) else meta.get("repository"))
    source, problem = npm_provenance(meta, integrity_hex)
    old_meta = npm_packument(change.name, change.old) if change.old else None
    if problem:
        checks.append(problem)
        status = "fail"
    elif source:
        if declared and source != declared:
            checks.append(f"provenance was built from {source}, but package.json declares {declared}")
            status = "fail"
        else:
            checks.append(f"SLSA provenance: built from {source} (sigstore bundle not verified here)")
    elif old_meta and (old_meta.get("dist") or {}).get("attestations"):
        checks.append(f"{change.old} had a provenance attestation, {change.new} has none")
        status = "fail"
    else:
        checks.append("no provenance attestation; registry signature only")
    old_data = http_get(old_meta["dist"]["tarball"]) if old_meta else None
    artifacts = (old_data, new_data, "tar") if old_data else None
    repo = source or declared
    found = find_tag(repo, change.name.rsplit("/", 1)[-1], change.new) if repo else None
    return artifacts, result(status, checks, repository=repo), release_notes(repo, [found[0]] if found else [])


def pypi_file(meta: dict) -> dict | None:
    files = meta.get("urls") or []
    for packagetype in ("sdist", "bdist_wheel"):
        for f in files:
            if f["packagetype"] == packagetype and (packagetype == "sdist" or f["filename"].endswith("-none-any.whl")):
                return f
    return files[0] if files else None


def pypi_source(meta: dict) -> str | None:
    urls = (meta.get("info") or {}).get("project_urls") or {}
    for key in sorted(urls, key=lambda k: (k.lower() not in ("source", "repository", "source code", "code"), k)):
        repo = github_repo(urls[key])
        if repo:
            return repo
    return github_repo((meta.get("info") or {}).get("home_page"))


def pypi_provenance(name: str, version: str, filename: str):
    data = try_json(f"https://pypi.org/integrity/{name}/{version}/{filename}/provenance",
                    "application/vnd.pypi.integrity.v1+json")
    if not data:
        return None
    publishers = [b.get("publisher") or {} for b in data.get("attestation_bundles", [])]
    return [github_repo("github.com/" + p["repository"]) for p in publishers if p.get("kind") == "GitHub" and p.get("repository")]


def review_pypi(change: Change, base: Path, head: Path):
    checks = []
    status = "pass"
    meta = try_json(f"https://pypi.org/pypi/{change.name}/{change.new}/json")
    if meta is None:
        return None, result("fail", [f"PyPI does not serve {change.name} {change.new} (404)"]), ""
    new_file = pypi_file(meta)
    if not new_file:
        return None, result("fail", [f"{change.name} {change.new} has no files on PyPI"]), ""
    new_data = http_get(new_file["url"])
    if hashlib.sha256(new_data).hexdigest() != new_file["digests"]["sha256"]:
        checks.append("downloaded file does not match the PyPI sha256 digest")
        status = "fail"
    repo = pypi_source(meta)
    publishers = pypi_provenance(change.name, change.new, new_file["filename"])
    old_meta = try_json(f"https://pypi.org/pypi/{change.name}/{change.old}/json") if change.old else None
    old_file = pypi_file(old_meta) if old_meta else None
    if publishers:
        if repo and repo not in publishers:
            checks.append(f"PEP 740 attestation publisher {publishers} is not the project's source {repo}")
            status = "fail"
        else:
            checks.append(f"PEP 740 attestation by trusted publisher {publishers[0]} (sigstore bundle not verified here)")
            repo = repo or publishers[0]
    elif old_file and pypi_provenance(change.name, change.old, old_file["filename"]):
        checks.append(f"{change.old} had a PEP 740 attestation, {change.new} has none")
        status = "fail"
    elif repo:
        found = find_tag(repo, change.name, change.new)
        if found:
            checks.append(f"no PEP 740 attestation; upstream tag `{found[0]}` exists in {repo}")
        else:
            checks.append(f"no PEP 740 attestation and no release tag for {change.new} in {repo}")
            status = "unverifiable"
    else:
        checks.append("no PEP 740 attestation and no GitHub source URL")
        status = "unverifiable"
    old_data = http_get(old_file["url"]) if old_file else None
    kind = "zip" if new_file["filename"].endswith(".whl") else "tar"
    if old_file and old_file["filename"].endswith(".whl") != new_file["filename"].endswith(".whl"):
        old_data = None
    artifacts = (old_data, new_data, kind) if old_data else None
    found = find_tag(repo, change.name, change.new) if repo else None
    return artifacts, result(status, checks, repository=repo, file=new_file["filename"]), release_notes(repo, [found[0]] if found else [])


REVIEWERS = {"action": review_action, "crate": review_crate, "npm": review_npm, "pypi": review_pypi}


# --------------------------------------------------------------------------


def collect(base: Path, head: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    changes, unsupported = detect(base, head)
    entries = []
    for change in changes:
        folder = out / change.id
        folder.mkdir(parents=True, exist_ok=True)
        entry = {"id": change.id, "ecosystem": change.ecosystem, "name": change.name,
                 "from": change.old, "to": change.new, "files": change.files}
        if change.ecosystem == "action":
            entry.update({"from_comment": change.old_comment, "to_comment": change.new_comment})
        try:
            reviewer = REVIEWERS[change.ecosystem]
            artifacts, provenance, notes = (reviewer(change) if change.ecosystem == "action"
                                            else reviewer(change, base, head))
        except (HttpError, ValueError, KeyError, OSError, RuntimeError, tarfile.TarError, zipfile.BadZipFile) as error:
            artifacts, provenance, notes = None, result("fail", [f"provenance check could not complete: {error}"]), ""
        entry["provenance"] = provenance
        (folder / "release-notes.md").write_text(notes or "No release notes.\n")
        if artifacts:
            old_bytes, new_bytes, kind = artifacts
            unpack(old_bytes, kind, folder / "old")
            unpack(new_bytes, kind, folder / "new")
            patch = diff_trees(folder / "old", folder / "new")
            entry["flags"] = scan_patch(patch)
            entry["diff_bytes"] = len(patch.encode())
            entry["diff_truncated"] = len(patch.encode()) > MAX_PATCH_BYTES
            (folder / "diff.patch").write_bytes(patch.encode()[:MAX_PATCH_BYTES])
        else:
            entry["flags"] = {}
            entry["diff"] = "none: newly added dependency or no comparable old artifact"
        (folder / "provenance.json").write_text(json.dumps(provenance, indent=2))
        entries.append(entry)
    summary = {"dependencies": entries, "unsupported_files": unsupported}
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", type=Path)
    parser.add_argument("--head", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--scan-patch", type=Path)
    args = parser.parse_args()
    if args.scan_patch:
        print(json.dumps(scan_patch(args.scan_patch.read_text(errors="replace")), indent=2))
        return 0
    if not (args.base and args.head and args.out):
        parser.error("--base, --head and --out are required")
    summary = collect(args.base, args.head, args.out)
    for entry in summary["dependencies"]:
        print(f"{entry['ecosystem']} {entry['name']} {entry['from']} -> {entry['to']}: "
              f"provenance {entry['provenance']['status']}; flags {sorted(entry['flags'])}")
    for rel in summary["unsupported_files"]:
        print(f"unsupported dependency file changed: {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
