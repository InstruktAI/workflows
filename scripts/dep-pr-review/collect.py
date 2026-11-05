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
# Over this the change cannot be reviewed completely and blocks. The
# environment override exists for the self-test.
MAX_PATCH_BYTES = int(os.environ.get("DEP_PR_REVIEW_MAX_PATCH_BYTES", 2 * 1024 * 1024))
MAX_NOTES_BYTES = 40 * 1024

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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from workflow_uses import Unparseable, workflow_uses  # noqa: E402
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


NEW_DEPENDENCY = ("new dependency: there is no previous publication to anchor its upstream identity on, "
                  "so a human must review it")


def tag_spellings(name: str, version: str) -> list[str]:
    bare = name.rsplit("/", 1)[-1]
    out = [f"v{version}", version]
    for n in dict.fromkeys([name, bare]):
        out += [f"{n}-v{version}", f"{n}-{version}", f"{n}@{version}", f"{n}/v{version}"]
    return out


def bind_release_tag(repo: str | None, ref: str | None, commit: str | None,
                     name: str, version: str) -> tuple[bool, str]:
    """A build is positive provenance only when it ran on the upstream
    release tag of exactly this version, and that tag still points to the
    commit it was built from. A branch build, or another tag, is not."""
    if not repo:
        return False, "the build names no GitHub source repository"
    if not ref or not ref.startswith("refs/tags/"):
        return False, f"built from `{ref}`, not from a release tag"
    tag = ref[len("refs/tags/"):]
    if tag.lower() not in {t.lower() for t in tag_spellings(name, version)}:
        return False, f"built from tag `{tag}`, which is not the release tag of {version}"
    upstream = tag_commit(repo, tag)
    if upstream is None:
        return False, f"build tag `{tag}` does not exist in {repo}"
    if commit and upstream != commit:
        return False, f"build commit {commit} is not upstream tag `{tag}` ({upstream})"
    return True, f"built from upstream release tag `{tag}` of {repo} at {upstream}"


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
    old_pin: dict | None = None   # the whole locked entry, locator included
    new_pin: dict = field(default_factory=dict)
    files: list[str] = field(default_factory=list)

    @property
    def old_comment(self) -> str | None:
        return (self.old_pin or {}).get("comment")

    @property
    def new_comment(self) -> str | None:
        return self.new_pin.get("comment")

    @property
    def id(self) -> str:
        # Both sides of the comparison: several old pins moving to one new
        # pin are separate comparisons and get separate directories.
        digest = hashlib.sha256(json.dumps([self.old_pin, self.new_pin], sort_keys=True).encode()).hexdigest()[:16]
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{self.ecosystem}-{self.name}-{self.new}")
        return f"{safe[:110]}-{digest}"


def walk(root: Path):
    """Every file of an exported tree. No directory is skipped: the trees
    hold exactly the tracked paths (export-tree.py), and a lockfile or
    submodule pointer under dist/, target/ or node_modules/ is as much a
    dependency change as one at the root."""
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            path = Path(dirpath) / filename
            yield path.relative_to(root).as_posix(), path


def uv_dist(entry: dict | None) -> dict | None:
    return {"url": entry.get("url"), "hash": entry.get("hash")} if entry else None


def read_pins(root: Path) -> dict[tuple[str, str, str], list[dict]]:
    """{(ecosystem, name, file): [pin, ...]}. A pin is the whole locked entry
    that decides what gets installed: the version plus where it comes from
    and its hash, so a changed URL or hash at the same version is an update
    too."""
    found: dict[tuple[str, str, str], list[dict]] = {}

    def add(ecosystem: str, name: str, rel: str, pin: dict):
        found.setdefault((ecosystem, name, rel), []).append(pin)

    def unhandled(rel: str, what: str, entry: object):
        """An entry this review cannot check: recorded so that adding or
        changing it blocks instead of vanishing."""
        add("unhandled", what, rel, {"version": hashlib.sha256(
            json.dumps(entry, sort_keys=True, default=str).encode()).hexdigest()[:16], "entry": entry})

    for rel, path in walk(root):
        base = path.name
        try:
            if base == "Cargo.lock":
                for package in tomllib.loads(path.read_text()).get("package", []):
                    source = package.get("source")
                    if source in CRATES_IO_SOURCES:
                        add("crate", package["name"], rel, {"version": package["version"],
                            "source": source, "checksum": package.get("checksum")})
                    elif source:
                        # git sources and alternate registries
                        unhandled(rel, f"crate {package['name']} from {source}", package)
            elif base == "uv.lock":
                for package in tomllib.loads(path.read_text()).get("package", []):
                    source = package.get("source") or {}
                    if source.get("registry") in PYPI_REGISTRIES:
                        add("pypi", package["name"].lower(), rel, {"version": package["version"],
                            "source": source["registry"], "sdist": uv_dist(package.get("sdist")),
                            "wheels": sorted((uv_dist(w) for w in package.get("wheels", [])), key=lambda w: w["url"] or "")})
                    elif set(source) & {"registry", "git", "url"} or not source:
                        unhandled(rel, f"python package {package['name']} from {source or 'an unknown source'}", package)
                    # editable, directory, path and virtual sources are this
                    # repository's own code, reviewed with the PR itself.
            elif base in ("package-lock.json", "npm-shrinkwrap.json"):
                data = json.loads(path.read_text())
                if "packages" not in data:
                    unhandled(rel, f"npm lockfile v{data.get('lockfileVersion', 1)} (only v2 and v3 are reviewed)", data)
                    continue
                for key, package in data["packages"].items():
                    if not key or package.get("link") or package.get("inBundle"):
                        # the root, workspace links, and files shipped inside
                        # their parent's tarball (reviewed with the parent)
                        continue
                    name = package.get("name") or key.rsplit("node_modules/", 1)[-1]
                    resolved = package.get("resolved")
                    if resolved and resolved.startswith("file:"):
                        continue
                    if "version" not in package or not resolved or not resolved.startswith(NPM_REGISTRY):
                        unhandled(rel, f"npm package {name} from {resolved or 'no resolved URL'}", package)
                        continue
                    add("npm", name, rel, {"version": package["version"], "resolved": resolved,
                                           "integrity": package.get("integrity")})
            elif base == "pnpm-lock.yaml":
                text = path.read_text()
                version = re.search(r"^lockfileVersion:\s*'?([^'\s]+)", text, re.MULTILINE)
                if not version or version[1] != "9.0":
                    unhandled(rel, f"pnpm lockfile version {version[1] if version else 'unknown'} (only 9.0 is reviewed)", text)
                    continue
                for name, version_, resolution, raw in pnpm_packages(text):
                    if name is None:
                        unhandled(rel, "pnpm packages entry this review cannot parse", raw)
                    elif set(resolution) == {"integrity"}:
                        add("npm", name, rel, {"version": version_, "integrity": resolution["integrity"]})
                    elif resolution.get("type") == "directory":
                        continue
                    else:
                        unhandled(rel, f"npm package {name} from {resolution}", raw)
            elif WORKFLOW_FILE.search(rel):
                # Every `uses` node of the parsed file, however its key is
                # quoted or its mapping styled; anything that is not a
                # reviewable owner/repo@ref blocks, never vanishes.
                try:
                    nodes = workflow_uses(path)
                except Unparseable as error:
                    unhandled(rel, f"workflow file that cannot be parsed ({error})", path.read_text())
                    continue
                for value, comment in nodes:
                    if isinstance(value, str) and value.startswith("./"):
                        continue
                    target, at, ref = value.partition("@") if isinstance(value, str) else ("", "", "")
                    if (not isinstance(value, str) or value.startswith("docker://") or not at
                            or not re.match(r"^[\w.-]+/[\w.-]+", target)):
                        unhandled(rel, f"action `{value}`", {"uses": value, "comment": comment})
                        continue
                    add("action", target, rel, {"version": ref, "comment": comment})
        except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError) as error:
            raise SystemExit(f"cannot parse {rel}: {error}")
    return found


PNPM_KEY = re.compile(r"^  '?/?((?:@[^@/\s]+/)?[^@\s'(]+)@([^\s'(:]+)[^:]*'?:\s*$")
PNPM_RESOLUTION = re.compile(r"^    resolution:\s*\{(.*)\}\s*$")


def pnpm_packages(text: str):
    """(name, version, resolution, raw) for each entry of a pnpm v9
    lockfile's `packages:` section; resolution is the parsed `{integrity:
    ...}`, `{tarball: ...}` or `{type: ..., ...}` mapping. An entry whose key
    or resolution cannot be parsed comes back with name None."""
    in_packages, current = False, None
    for line in text.splitlines():
        if re.match(r"^\S", line):
            if current:
                yield None, None, {}, current
            in_packages = line.rstrip() == "packages:"
            current = None
            continue
        if not in_packages or not line.strip():
            continue
        if re.match(r"^  \S", line):
            if current:
                yield None, None, {}, current
            key = PNPM_KEY.match(line)
            current = (key[1], key[2]) if key else line
            continue
        res = PNPM_RESOLUTION.match(line)
        if res and isinstance(current, tuple):
            resolution = dict(
                (k.strip(), v.strip().strip("'\""))
                for k, _, v in (part.partition(":") for part in res[1].split(","))
            )
            yield current[0], current[1], resolution, line
            current = None
    if current:
        yield None, None, {}, current


def changed_files(base: Path, head: Path) -> list[str]:
    def digests(root):
        return {rel: hashlib.sha256(p.read_bytes()).hexdigest() for rel, p in walk(root) if p.is_file()}
    old, new = digests(base), digests(head)
    return sorted(rel for rel in set(old) | set(new) if old.get(rel) != new.get(rel))


def major(version: str) -> str:
    match = re.match(r"v?(\d+)", version)
    return match[1] if match else version


def pair(new_pin: dict, removed: list[dict]) -> dict | None:
    """The old pin this new one replaces: same version (a changed locator),
    else the only one of the same major version, else the only one."""
    for rule in (lambda p: p["version"] == new_pin["version"],
                 lambda p: major(p["version"]) == major(new_pin["version"]),
                 lambda p: True):
        candidates = [p for p in removed if rule(p)]
        if len(candidates) == 1:
            return candidates[0]
    return None


def detect(base: Path, head: Path) -> tuple[list[Change], list[str]]:
    """Updates are found per file: a pin added to one lockfile or workflow
    is an update even when another file already used it."""
    old_all, new_all = read_pins(base), read_pins(head)
    merged: dict[str, Change] = {}
    for key, new_pins in sorted(new_all.items()):
        ecosystem, name, rel = key
        old_pins = old_all.get(key, [])
        old_keys = {json.dumps(p, sort_keys=True) for p in old_pins}
        new_keys = {json.dumps(p, sort_keys=True) for p in new_pins}
        removed = [p for p in old_pins if json.dumps(p, sort_keys=True) not in new_keys]
        for pin in new_pins:
            if json.dumps(pin, sort_keys=True) in old_keys:
                continue
            old = pair(pin, removed)
            ident = json.dumps([ecosystem, name, old, pin], sort_keys=True)
            if ident not in merged:
                merged[ident] = Change(ecosystem, name, old["version"] if old else None, pin["version"],
                                       old_pin=old, new_pin=pin)
            merged[ident].files.append(rel)
    changed = changed_files(base, head)
    unsupported = [rel for rel in changed if UNSUPPORTED.search(rel)]
    # export-tree.py writes a submodule pointer as `gitlink <commit>`: a
    # changed pointer is a dependency update this review cannot check.
    for rel in changed:
        if any((root / rel).is_file() and (root / rel).read_bytes().startswith(b"gitlink ") for root in (base, head)):
            unsupported.append(f"submodule {rel}")
    return list(merged.values()), unsupported


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
    # A .git directory inside an artifact is removed (named in a note file):
    # the model's workspace must contain no git repository.
    for dirpath, dirnames, _ in os.walk(dest):
        if ".git" in dirnames:
            shutil.rmtree(Path(dirpath) / ".git")
            Path(dirpath, ".git.removed-by-dep-pr-review").write_text("a .git directory was removed here\n")
            dirnames.remove(".git")
    # Symlinks become plain files naming their target: the bundle is checked
    # file by file after upload, and a link must not point outside it.
    for dirpath, dirnames, filenames in os.walk(dest):
        for name in dirnames + filenames:
            link = Path(dirpath) / name
            if link.is_symlink():
                target = os.readlink(link)
                link.unlink()
                link.write_text(f"symlink -> {target}\n")
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
        r"ACTIONS_RUNTIME_TOKEN|ACTIONS_ID_TOKEN|github\.token|AWS_[A-Z_]+|\.ssh/|id_rsa|\.npmrc|\.pypirc|"
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
    if change.old is None:
        checks.append(NEW_DEPENDENCY)
        status = "unverifiable" if status == "pass" else status
    tags = [t for t in (change.old_comment, version) if t]
    return fetch_action(repo, change), result(status, checks, repository=repo, tag=version), release_notes(repo, tags)


def fetch_action(repo: str, change: Change):
    """(old, new, kind); old is None for a new action or an old ref that is
    not a commit (collect() blocks that case: it cannot be diffed exactly)."""
    if not FULL_SHA.match(change.new):
        return None
    new = http_get(f"https://codeload.github.com/{repo}/tar.gz/{change.new}")
    old = None
    if change.old and FULL_SHA.match(change.old):
        old = http_get(f"https://codeload.github.com/{repo}/tar.gz/{change.old}")
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
    # The lockfile entry must lock exactly the artifact reviewed here.
    locked = change.new_pin.get("checksum")
    if locked != new["sha256"]:
        checks.append(f"Cargo.lock checksum `{locked}` is not the reviewed crates.io artifact `{new['sha256']}`")
        status = "fail"
    else:
        checks.append("Cargo.lock checksum is the reviewed crates.io artifact")
    # Upstream identity is anchored on the PREVIOUS version's .crate.
    old_data, old = (crate_files(change.name, change.old) if change.old else (None, None))
    repo = github_repo(new["repository"])
    # Every reason the result is not positive is recorded, so settlement can
    # tell whether "no release tag of its own" is the only one left.
    open_issues: list[str] = []
    if old is None:
        checks.append(NEW_DEPENDENCY)
        open_issues.append("new-dependency")
        status = "unverifiable" if status == "pass" else status
    elif github_repo(old["repository"]) != repo:
        checks.append(f"repository changed: `{old['repository']}` -> `{new['repository']}`")
        status = "fail"
    else:
        checks.append(f"repository `{new['repository']}` matches the previous version")
    vcs = new["vcs"]
    sha = (vcs or {}).get("git", {}).get("sha1")
    tag = None
    if not sha:
        checks.append("the .crate has no .cargo_vcs_info.json commit; the source cannot be tied to a commit")
        open_issues.append("no-vcs-info")
        status = "fail" if status == "fail" else "unverifiable"
    elif not repo:
        checks.append(f"repository `{new['repository']}` is not on GitHub; commit {sha} not checked")
        open_issues.append("not-github")
        status = "fail" if status == "fail" else "unverifiable"
    else:
        if vcs.get("git", {}).get("dirty"):
            # The worktree had changes the commit does not hold: no tag,
            # own or a sibling's, can vouch for what was packaged.
            checks.append("packaged from a dirty worktree")
            open_issues.append("dirty")
            status = "fail" if status == "fail" else "unverifiable"
        found = find_tag(repo, change.name, change.new)
        if found and found[1] == sha:
            tag = found[0]
            checks.append(f".cargo_vcs_info.json commit {sha} is upstream tag `{tag}`")
        elif found:
            checks.append(f".cargo_vcs_info.json commit {sha} is not upstream tag `{found[0]}` ({found[1]})")
            status = "fail"
        elif commit_exists(repo, sha):
            # Settled in collect() when a sibling crate of the same
            # repository in this PR has a verified release tag containing it.
            checks.append(f"commit {sha} exists upstream but no release tag for {change.new} was found")
            open_issues.append("no-release-tag")
            status = "fail" if status == "fail" else "unverifiable"
        else:
            checks.append(f"commit {sha} does not exist in {repo}")
            status = "fail"
    artifacts = (old_data, new_data, "tar")
    tags = [t for t in (tag,) if t]
    facts = {"repository": new["repository"], "commit": sha, "tag": tag}
    # Settlement in collect() may resolve exactly one condition: "no release
    # tag of its own", and only when it is the sole open issue.
    if status == "unverifiable" and open_issues == ["no-release-tag"]:
        facts["companion_candidate"] = True
    return artifacts, result(status, checks, **facts), release_notes(repo, tags)


def npm_packument(name: str, version: str):
    quoted = name.replace("/", "%2f")
    return http_json(f"{NPM_REGISTRY}{quoted}/{version}")


def npm_tarball_url(name: str, version: str) -> str:
    return f"{NPM_REGISTRY}{name}/-/{name.rsplit('/', 1)[-1]}-{version}.tgz"


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


def npm_provenance(meta: dict) -> tuple[dict | None, str | None]:
    """({repo, ref, commit, digest}, problem) from the SLSA provenance
    attestation npm serves at dist.attestations.url (statement shape as
    served by registry.npmjs.org)."""
    attestations = (meta.get("dist") or {}).get("attestations")
    if not attestations:
        return None, None
    bundle = http_json(attestations["url"])
    for attestation in bundle.get("attestations", []):
        if "slsa.dev/provenance" not in attestation.get("predicateType", ""):
            continue
        statement = json.loads(base64.b64decode(attestation["bundle"]["dsseEnvelope"]["payload"]))
        build = statement["predicate"]["buildDefinition"]
        workflow = build["externalParameters"]["workflow"]
        commits = [d.get("digest", {}).get("gitCommit") for d in build.get("resolvedDependencies") or []]
        return {"repo": github_repo(workflow.get("repository")), "ref": workflow.get("ref"),
                "commit": next((c for c in commits if c), None),
                "digests": [s.get("digest", {}).get("sha512") for s in statement.get("subject", [])]}, None
    return None, "attestations listed but no SLSA provenance among them"


def npm_repository(meta: dict) -> str | None:
    repository = meta.get("repository")
    return github_repo(repository.get("url") if isinstance(repository, dict) else repository)


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
    # The lockfile entry must lock exactly the artifact reviewed here: the
    # default registry's tarball URL for name@version, and its integrity.
    expected_url = npm_tarball_url(change.name, change.new)
    resolved = change.new_pin.get("resolved")
    if resolved is not None and resolved != expected_url:
        checks.append(f"locked URL `{resolved}` is not the registry tarball `{expected_url}`")
        status = "fail"
    if dist["tarball"] != expected_url:
        checks.append(f"registry tarball `{dist['tarball']}` is not the default URL `{expected_url}`")
        status = "fail"
    if change.new_pin.get("integrity") != integrity:
        checks.append(f"locked integrity `{change.new_pin.get('integrity')}` is not the registry's `{integrity}`")
        status = "fail"
    else:
        checks.append("locked URL and integrity are the reviewed registry tarball")
    problem = verify_npm_signature(change.name, change.new, dist, keys)
    if problem:
        checks.append(problem)
        status = "fail"
    else:
        checks.append("npm registry signature verifies (ECDSA P-256 over name@version:integrity)")
    # Upstream identity is anchored on the PREVIOUS version's publication:
    # its declared repository and, where it had one, its provenance source.
    old_meta = npm_packument(change.name, change.old) if change.old else None
    declared = npm_repository(meta)
    provenance, problem = npm_provenance(meta)
    old_provenance = npm_provenance(old_meta)[0] if old_meta else None
    anchor = (old_provenance or {}).get("repo") or (npm_repository(old_meta) if old_meta else None)
    repo = anchor
    status_before = status
    if old_meta is None:
        checks.append(NEW_DEPENDENCY)
        status = "unverifiable" if status == "pass" else status
    elif anchor is None:
        checks.append(f"{change.old} names no GitHub source repository to anchor {change.new} on")
        status = "unverifiable" if status == "pass" else status
    elif declared != anchor:
        checks.append(f"upstream identity changed: {change.old} came from {anchor}, {change.new} declares {declared}")
        status = "fail"
    if problem:
        checks.append(problem)
        status = "fail"
    elif provenance:
        if integrity_hex not in provenance["digests"]:
            checks.append("provenance subject digest does not match the published tarball")
            status = "fail"
        if anchor and provenance["repo"] != anchor:
            checks.append(f"provenance was built from {provenance['repo']}, not from {anchor}")
            status = "fail"
        ok, message = bind_release_tag(provenance["repo"], provenance["ref"], provenance["commit"],
                                       change.name, change.new)
        checks.append(f"SLSA provenance: {message} (sigstore bundle not verified here)")
        if not ok:
            status = "fail"
    elif old_provenance:
        checks.append(f"{change.old} had a provenance attestation, {change.new} has none")
        status = "fail"
    elif status == status_before and anchor:
        found = find_tag(anchor, change.name, change.new)
        if found:
            checks.append(f"no provenance attestation; upstream release tag `{found[0]}` exists in {anchor}")
        else:
            checks.append(f"no provenance attestation and no release tag for {change.new} in {anchor}")
            status = "unverifiable" if status == "pass" else status
    old_data = http_get(old_meta["dist"]["tarball"]) if old_meta else None
    artifacts = (old_data, new_data, "tar")
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


# Fulcio certificate extensions (sigstore fulcio docs/oid-info.md):
# 1.3.6.1.4.1.57264.1.12 source repository URI, .13 source repository
# digest (the commit), .14 source repository ref.
FULCIO = {"12": "repo", "13": "commit", "14": "ref"}


def fulcio_claims(certificate_b64: str) -> dict:
    """Source repository, commit and ref from a sigstore signing
    certificate, read with openssl. The certificate chain is not verified
    here; PyPI verifies attestations when they are uploaded."""
    with tempfile.TemporaryDirectory() as tmp:
        der = Path(tmp, "cert.der")
        der.write_bytes(base64.b64decode(certificate_b64))
        text = subprocess.run(["openssl", "x509", "-inform", "DER", "-noout", "-text", "-in", str(der)],
                              capture_output=True, text=True, check=True).stdout
    claims = {}
    for oid, key in FULCIO.items():
        match = re.search(rf"1\.3\.6\.1\.4\.1\.57264\.1\.{oid}:\s*\n\s*(.*)", text)
        if match:
            value = match[1]
            pattern = {"repo": r"https://\S+", "commit": r"[0-9a-f]{40}", "ref": r"refs/\S+"}[key]
            found = re.search(pattern, value)
            claims[key] = found[0] if found else None
    claims["repo"] = github_repo(claims.get("repo"))
    return claims


def pypi_provenance(name: str, version: str, filename: str) -> dict | None:
    """{publishers: [owner/repo], claims: [{repo, commit, ref}]} from PyPI's
    integrity API (docs.pypi.org/api/integrity), or None without any."""
    data = try_json(f"https://pypi.org/integrity/{name}/{version}/{filename}/provenance",
                    "application/vnd.pypi.integrity.v1+json")
    if not data:
        return None
    publishers, claims = [], []
    for bundle in data.get("attestation_bundles", []):
        publisher = bundle.get("publisher") or {}
        if publisher.get("kind") == "GitHub" and publisher.get("repository"):
            publishers.append(github_repo("github.com/" + publisher["repository"]))
        for attestation in bundle.get("attestations", []):
            certificate = (attestation.get("verification_material") or {}).get("certificate")
            if certificate:
                claims.append(fulcio_claims(certificate))
    return {"publishers": sorted(set(publishers)), "claims": claims} if publishers or claims else None


def dist_key(filename: str, version: str) -> str:
    """What pairs a distribution with its counterpart in another version:
    `sdist`, or a wheel's tags (the part after name-version-)."""
    if not filename.endswith(".whl"):
        return "sdist"
    parts = filename[:-4].split("-")
    return "wheel-" + "-".join(parts[2:]) if len(parts) >= 5 else f"wheel-{filename}"


def locked_dists(pin: dict | None) -> list[dict]:
    return [d for d in [(pin or {}).get("sdist"), *(pin or {}).get("wheels", [])] if d]


def review_pypi(change: Change, base: Path, head: Path):
    """Every distribution uv.lock records for the new version is checked and
    reviewed on its own: consumers install wheels, not the sdist."""
    checks = []
    status = "pass"
    meta = try_json(f"https://pypi.org/pypi/{change.name}/{change.new}/json")
    if meta is None:
        return None, result("fail", [f"PyPI does not serve {change.name} {change.new} (404)"]), ""
    served = {f["filename"]: f for f in meta.get("urls") or []}
    locked = locked_dists(change.new_pin)
    if not locked:
        return None, result("fail", ["uv.lock records no distribution for this version"]), ""
    old_meta = try_json(f"https://pypi.org/pypi/{change.name}/{change.old}/json") if change.old else None
    old_served = {dist_key(f["filename"], change.old): f for f in (old_meta or {}).get("urls") or []}
    artifacts = []
    attested: dict[str, list[str] | None] = {}
    for dist in locked:
        filename = urllib.parse.unquote((dist.get("url") or "").rsplit("/", 1)[-1])
        real = served.get(filename)
        # The locked file must be the file PyPI serves for this name and
        # version, at PyPI's URL, with PyPI's digest, and download as such.
        if not real:
            checks.append(f"locked file `{dist.get('url')}` is not a file PyPI serves for {change.name} {change.new}")
            status = "fail"
            continue
        if dist.get("url") != real["url"] or dist.get("hash") != f"sha256:{real['digests']['sha256']}":
            checks.append(f"locked `{filename}` does not match PyPI's URL and sha256")
            status = "fail"
            continue
        data = http_get(real["url"])
        if hashlib.sha256(data).hexdigest() != real["digests"]["sha256"]:
            checks.append(f"downloaded `{filename}` does not match its sha256")
            status = "fail"
            continue
        attested[filename] = pypi_provenance(change.name, change.new, filename)
        key = dist_key(filename, change.new)
        old_file = old_served.get(key)
        old_data = http_get(old_file["url"]) if old_file else None
        if old_file and hashlib.sha256(old_data).hexdigest() != old_file["digests"]["sha256"]:
            checks.append(f"downloaded previous `{old_file['filename']}` does not match its sha256")
            status = "fail"
        artifacts.append((key, old_data, data, "zip" if filename.endswith(".whl") else "tar"))
    if status == "pass":
        checks.append(f"all {len(locked)} locked distributions are PyPI's files, downloaded with PyPI's sha256")
    # Attestation per file: a release built by trusted publishing attests
    # every file it uploads, so a file without one next to files with one
    # was uploaded some other way.
    with_attestation = sorted(f for f, info in attested.items() if info)
    without = sorted(f for f, info in attested.items() if not info)
    if with_attestation and without:
        checks.append(f"{len(without)} of {len(attested)} files have no PEP 740 attestation: {', '.join(without)}")
        status = "fail"
    publishers = sorted({p for f in with_attestation for p in attested[f]["publishers"]})
    # Upstream identity is anchored on the PREVIOUS version's publication:
    # its source repository and, where it had them, its trusted publishers.
    old_file = old_served.get("sdist") or next(iter(old_served.values()), None)
    old_attested = pypi_provenance(change.name, change.old, old_file["filename"]) if old_file else None
    anchor_publishers = (old_attested or {}).get("publishers") or []
    anchor = (anchor_publishers[0] if anchor_publishers else None) or (pypi_source(old_meta) if old_meta else None)
    repo = anchor
    declared = pypi_source(meta)
    if old_meta is None:
        checks.append(NEW_DEPENDENCY)
        status = "unverifiable" if status == "pass" else status
    elif anchor is None:
        checks.append(f"{change.old} names no GitHub source repository to anchor {change.new} on")
        status = "unverifiable" if status == "pass" else status
    else:
        if declared and declared != anchor and declared not in anchor_publishers:
            checks.append(f"upstream identity changed: {change.old} came from {anchor}, {change.new} declares {declared}")
            status = "fail"
        if anchor_publishers and publishers != anchor_publishers:
            checks.append(f"trusted publisher changed: {change.old} by {anchor_publishers}, {change.new} by {publishers or 'none'}")
            status = "fail"
    # Every new publisher and every signing certificate's repository must be
    # the previous publication's trusted publisher. A first attestation has
    # nothing to compare with: project_urls are self-declared and carry over
    # unchanged, so they cannot vouch for who attests now.
    if publishers and old_meta is not None and not anchor_publishers:
        checks.append(f"publisher transition: {change.new} is attested by {', '.join(publishers)}, but "
                      f"{change.old} had no trusted publisher to anchor on, so a human must confirm it")
        status = "unverifiable" if status == "pass" else status
    for f in with_attestation:
        for claim in attested[f]["claims"]:
            if anchor_publishers and claim.get("repo") not in anchor_publishers:
                checks.append(f"`{f}` was signed for repository {claim.get('repo')}, not for {', '.join(anchor_publishers)}")
                status = "fail"
    if publishers:
        bound = True
        for f in with_attestation:
            for claim in attested[f]["claims"] or [{}]:
                ok, message = bind_release_tag(claim.get("repo"), claim.get("ref"), claim.get("commit"),
                                               change.name, change.new)
                if not ok:
                    checks.append(f"`{f}`: {message}")
                    status, bound = "fail", False
        if bound:
            claim = attested[with_attestation[0]]["claims"][0]
            checks.append(f"every file has a PEP 740 attestation by {publishers[0]}, "
                          f"built from release tag `{claim['ref'][len('refs/tags/'):]}` at {claim['commit']} "
                          "(sigstore bundle not verified here)")
    elif old_attested:
        checks.append(f"{change.old} had a PEP 740 attestation, {change.new} has none")
        status = "fail"
    elif anchor and status == "pass":
        found = find_tag(anchor, change.name, change.new)
        if found:
            checks.append(f"no PEP 740 attestation; upstream release tag `{found[0]}` exists in {anchor}")
        else:
            checks.append(f"no PEP 740 attestation and no release tag for {change.new} in {anchor}")
            status = "unverifiable"
    found = find_tag(repo, change.name, change.new) if repo else None
    return (artifacts or None), result(status, checks, repository=repo, files=sorted(attested)), \
        release_notes(repo, [found[0]] if found else [])


def review_unhandled(change: Change, base: Path, head: Path):
    return None, result("fail", [f"unhandled dependency entry, not reviewed: {change.name}"]), ""


REVIEWERS = {"action": review_action, "crate": review_crate, "npm": review_npm, "pypi": review_pypi,
             "unhandled": review_unhandled}


# --------------------------------------------------------------------------


def collect(base: Path, head: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    changes, unsupported = detect(base, head)
    entries = []
    for change in changes:
        folder = out / change.id
        if folder.exists():
            raise SystemExit(f"two comparisons map to one bundle directory: {change.id}")
        folder.mkdir(parents=True)
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
        # Anything that keeps the model from seeing the complete change
        # blocks the PR, whatever the verdict.
        blockers: list[str] = []
        if artifacts:
            parts = artifacts if isinstance(artifacts, list) else [("", *artifacts)]
            if change.old is None:
                entry["baseline"] = "empty: new dependency, its whole published artifact is the change"
            chunks, seen = [], {}
            for label, old_bytes, new_bytes, kind in parts:
                where = folder / label if label else folder
                if old_bytes is None:
                    (where / "old").mkdir(parents=True, exist_ok=True)
                    if change.old is not None and not label:
                        blockers.append(f"the previous `{change.old}` cannot be fetched as an exact artifact "
                                        "(not a commit SHA), so the change cannot be diffed")
                    elif change.old is not None:
                        entry.setdefault("new_distributions", []).append(label)
                else:
                    unpack(old_bytes, kind, where / "old")
                unpack(new_bytes, kind, where / "new")
                text = diff_trees(where / "old", where / "new")
                if label:
                    # Wheels of one release mostly repeat the same files: a
                    # file change already shown for another distribution is
                    # named, not repeated.
                    kept = []
                    for block in re.split(r"(?m)^(?=diff -ruN )", text):
                        digest = hashlib.sha256(re.sub(r"(?m)^(diff -ruN|---|\+\+\+) .*$", "", block).encode()).hexdigest()
                        if block.strip() and digest in seen:
                            first = block.splitlines()[0] if block else ""
                            kept.append(f"{first}\n(same change as in distribution {seen[digest]})\n")
                        else:
                            seen.setdefault(digest, label)
                            kept.append(block)
                    text = f"### distribution {label}\n" + "".join(kept)
                chunks.append(text)
            patch = "".join(chunks).encode()
            entry["flags"] = scan_patch(patch.decode(errors="replace"))
            entry["diff_bytes"] = len(patch)
            if len(patch) > MAX_PATCH_BYTES:
                blockers.append(f"the diff is {len(patch)} bytes, over the {MAX_PATCH_BYTES}-byte review cap")
            (folder / "diff.patch").write_bytes(patch[:MAX_PATCH_BYTES])
        else:
            entry["flags"] = {}
            if change.ecosystem != "unhandled":
                blockers.append("no published artifact could be fetched to review")
        entry["review_blockers"] = blockers
        (folder / "provenance.json").write_text(json.dumps(provenance, indent=2))
        entries.append(entry)
    settle_companions(entries, out)
    summary = {"dependencies": entries, "unsupported_files": unsupported}
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    write_manifest(out)
    return summary


def settle_companions(entries: list[dict], out: Path):
    """A workspace crate published without a tag of its own (time-core next
    to time, say) is positive only when its .cargo_vcs_info.json commit is
    contained in a release tag already verified, in this same PR, for a
    sibling crate of the same anchored repository. Nothing else qualifies."""
    verified = [e for e in entries if e["ecosystem"] == "crate" and e["provenance"]["status"] == "pass"
                and e["provenance"].get("tag")]
    for entry in entries:
        prov = entry["provenance"]
        if not prov.pop("companion_candidate", False) or prov["status"] != "unverifiable":
            continue
        repo = github_repo(prov.get("repository"))
        for sibling in verified:
            if github_repo(sibling["provenance"].get("repository")) != repo:
                continue
            tag = sibling["provenance"]["tag"]
            compare = try_json(f"{GITHUB_API}/repos/{repo}/compare/{prov['commit']}...{urllib.parse.quote(tag, safe='')}")
            if compare and compare.get("status") in ("ahead", "identical"):
                prov["checks"].append(f"commit {prov['commit']} is contained in release tag `{tag}`, verified in "
                                      f"this PR for sibling crate {sibling['name']} {sibling['to']} of the same repository")
                prov["status"] = "pass"
                prov["companion_of"] = f"{sibling['name']} {sibling['to']}"
                break
        (out / entry["id"] / "provenance.json").write_text(json.dumps(prov, indent=2))


def write_manifest(out: Path):
    """sha256 of every file in the bundle, hidden ones included, in
    `sha256sum` format, so a download can be checked for completeness."""
    lines = []
    for rel, path in sorted(walk_all(out)):
        if rel != "MANIFEST.sha256":
            lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  ./{rel}\n")
    (out / "MANIFEST.sha256").write_text("".join(lines))


def walk_all(root: Path):
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.is_file() and not path.is_symlink():
                yield path.relative_to(root).as_posix(), path


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
