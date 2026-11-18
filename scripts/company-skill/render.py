#!/usr/bin/env python3
"""Render a company's knowledge into its committed AGENTS.md and its Claude plugin.

A company's repository is its home: `AGENTS.master.md` at the root holds the
company's knowledge master, and the documents it draws on sit under `docs/`.
From those, and from nothing else, this script writes:

- `AGENTS.md` at the root: the master with its required reads inlined, and the
  fixed passage that tells an agent where the knowledge comes from and how to
  improve it;
- `plugins/<slug>/`: a Claude plugin with one skill holding the same knowledge
  (the internal skill: every document, whatever its visibility), with the
  plugin's version raised whenever its rendering changes;
- the plugin's entry in `.claude-plugin/marketplace.json`, by relative path.

The output is a function of the committed documents and the plugin's previous
version only, so the same documents give the same bytes and "commit only when
changed" is an exact comparison.

This is the stand-in for the instrukt command that will render company skills;
it reads and writes the same files, so that command can replace it without any
change in a company's repository.

Usage: render.py --root DIR --name NAME (--slug SLUG | --repository OWNER/company-SLUG) [--check | --validate]
  --check     write nothing; exit 1 and name each file whose committed content
              differs from the rendering.
  --validate  write nothing; exit 1 only on a finding in the documents.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TypeAlias

MASTER = "AGENTS.master.md"
ENTRYPOINT = "AGENTS.md"
DOCS = "docs"
CATALOG = ".claude-plugin/marketplace.json"
FRONTMATTER_KEYS = {"description", "visibility"}
FIRST_VERSION = "0.1.0"
MAX_DESCRIPTION_CHARS = 1024
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

LINK = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)\)")
FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,6}) ")
# Text that reaches outside the company's own documents: an @-include, a path
# that starts in a home folder (`~/`), an `instrukt docs` or `telec docs` read,
# or a snippet id of the shared `general/` or `sdlc/` doctrine. Markdown links
# are checked separately (see check_self_contained).
OUTSIDE = (
    (re.compile(r"(^|\s)@[~/.]"), "an @-include"),
    (re.compile(r"(^|[\s(`])~/"), "a path in a home folder"),
    (re.compile(r"\binstrukt docs\b"), "an instrukt docs read"),
    (re.compile(r"\btelec docs\b"), "a docs read of another platform"),
    (re.compile(r"(^|[\s`\[(])(general|sdlc)/[a-z0-9-]+/"), "a document of the shared doctrine"),
)


JsonValue: TypeAlias = "str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]"
JsonObject: TypeAlias = "dict[str, JsonValue]"


class RenderError(Exception):
    pass


class Visibility(StrEnum):
    """Who a document is for. The internal skill holds every document; a public one only `public` documents."""

    INTERNAL = "internal"
    PUBLIC = "public"


@dataclass(frozen=True)
class Catalog:
    """The plugin catalog: the plugin entries this renderer reads, and the rest of the file kept as it is."""

    document: JsonObject
    plugins: list[JsonValue]

    @classmethod
    def parse(cls, text: str) -> "Catalog":
        document = json.loads(text)
        if not isinstance(document, dict):
            raise RenderError(f"{CATALOG}: expected an object")
        plugins = document.get("plugins")
        if not isinstance(plugins, list):
            raise RenderError(f"{CATALOG}: 'plugins' must be a list")
        return cls(document, plugins)

    def entry(self, name: str) -> JsonObject | None:
        """The entry listed under this name; entries of other shapes are kept and never read."""
        for plugin in self.plugins:
            if isinstance(plugin, dict) and plugin.get("name") == name:
                return plugin
        return None


def manifest_version(text: str) -> str:
    """The `version` a committed plugin.json carries."""
    manifest = json.loads(text)
    version = manifest.get("version") if isinstance(manifest, dict) else None
    if not isinstance(version, str):
        raise RenderError("the committed plugin.json has no 'version' string")
    return version


@dataclass(frozen=True)
class Document:
    path: PurePosixPath  # relative to the repository root, under docs/
    description: str
    visibility: Visibility
    body: str

    @property
    def reference(self) -> str:
        """The file name this document gets under the skill's references/."""
        return "-".join(self.path.relative_to(DOCS).with_suffix("").parts) + ".md"


def split_frontmatter(text: str, where: str) -> tuple[dict[str, str], str]:
    match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return {}, text
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep or line.startswith((" ", "\t")):
            raise RenderError(f"{where}: frontmatter line '{line}' is not 'key: value'")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        meta[key.strip()] = value
    return meta, text[match.end() :]


def outside_code(lines: list[str]) -> Iterator[tuple[int, str]]:
    """Yield (index, line) for every line outside fenced code."""
    fenced = False
    for index, line in enumerate(lines):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            yield index, line


def check_self_contained(text: str, where: str, root: Path, base: PurePosixPath) -> list[str]:
    """Findings for every reference that leaves the company's own documents."""
    findings = []
    for index, line in outside_code(text.splitlines()):
        for pattern, what in OUTSIDE:
            if pattern.search(line):
                findings.append(f"{where}:{index + 1}: {what} reaches outside the company's own documents: {line.strip()}")
        for _, target in LINK.findall(line):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
                continue  # a web address or an anchor is not a document
            resolved = resolve(base, target)
            if resolved is None or not (root / resolved).is_file() or resolved.parts[0] != DOCS:
                findings.append(
                    f"{where}:{index + 1}: link '{target}' is not one of the company's documents under {DOCS}/"
                )
    return findings


def resolve(base: PurePosixPath, target: str) -> PurePosixPath | None:
    """A link target relative to the folder `base`, as a path from the root; None when it leaves the root."""
    target = target.split("#", 1)[0]
    if not target or target.startswith("/"):
        return None
    parts: list[str] = list(base.parts)
    for part in PurePosixPath(target).parts:
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part != ".":
            parts.append(part)
    return PurePosixPath(*parts) if parts else None


def demote(body: str, levels: int) -> str:
    lines = body.splitlines()
    for index, line in outside_code(lines):
        match = HEADING.match(line)
        if match:
            depth = min(6, len(match.group(1)) + levels)
            lines[index] = "#" * depth + line[len(match.group(1)) :]
    return "\n".join(lines) + "\n"


def without_comments(text: str) -> str:
    """Drop HTML comments that occupy whole lines, such as a master's lock notes, also over several lines."""
    lines: list[str] = []
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if inside:
            inside = "-->" not in stripped
            continue
        if stripped.startswith("<!--"):
            inside = "-->" not in stripped[4:]
            continue
        lines.append(line)
    return "\n".join(lines).strip() + "\n"


def read_documents(root: Path) -> dict[PurePosixPath, Document]:
    documents: dict[PurePosixPath, Document] = {}
    findings: list[str] = []
    for path in sorted((root / DOCS).rglob("*.md")):
        relative = PurePosixPath(path.relative_to(root).as_posix())
        meta, body = split_frontmatter(path.read_text(encoding="utf-8"), str(relative))
        unknown = sorted(set(meta) - FRONTMATTER_KEYS)
        if unknown:
            findings.append(f"{relative}: unknown frontmatter keys {unknown}; allowed: {sorted(FRONTMATTER_KEYS)}")
        description = meta.get("description", "")
        if not description:
            findings.append(f"{relative}: frontmatter needs a non-empty 'description'")
        visibility = Visibility.INTERNAL
        if "visibility" in meta:
            try:
                visibility = Visibility(meta["visibility"])
            except ValueError:
                findings.append(
                    f"{relative}: visibility '{meta['visibility']}' is not one of {[member.value for member in Visibility]}"
                )
        documents[relative] = Document(relative, description, visibility, without_comments(body))
    if findings:
        raise RenderError("\n".join(findings))
    return documents


def read_master(root: Path) -> tuple[str, list[PurePosixPath]]:
    """The master's content without its required reads, and the documents it requires, in order."""
    path = root / MASTER
    if not path.is_file():
        raise RenderError(f"{MASTER} is missing: it is the company's knowledge master")
    text = without_comments(path.read_text(encoding="utf-8"))
    match = re.search(r"^## Required reads\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if not match:
        return text, []
    required: list[PurePosixPath] = []
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        item = re.fullmatch(r"- \[[^\]]*\]\(([^)\s]+)\)", line.strip())
        target = resolve(PurePosixPath(), item.group(1)) if item else None
        if target is None:
            raise RenderError(f"{MASTER}: required read '{line.strip()}' is not a link '- [title](docs/<file>.md)'")
        required.append(target)
    rest = (text[: match.start()] + text[match.end() :]).strip() + "\n"
    return rest, required


def passage(name: str, slug: str) -> str:
    """The fixed passage every rendering carries. It names a location, never a host or an organization."""
    return f"""## Where this knowledge comes from

This knowledge is rendered from the documents of {name}'s repository, which is
{name}'s company home: `{MASTER}` at its root and the documents under `{DOCS}/`
beside it. On a computer or in a container that runs instrukt, the company home
is the checkout of that repository at `~/.instrukt/companies/{slug}/`.

An agent that has this repository checked out improves the knowledge by
changing those documents there and committing the change. It never edits what
is generated from them: `{ENTRYPOINT}` and everything under `plugins/{slug}/`.
Once the commit reaches the repository's default branch, a workflow renders
the knowledge again and publishes it; nothing else needs to be done.

A document states what is true of {name} and references nothing outside the
company's own documents. Settings, secrets and other projects in the company
home are not knowledge and are never committed.
"""


@dataclass(frozen=True)
class Rendering:
    files: dict[str, str]  # path from the root -> content
    plugin_dir: str


def plugin_description(name: str) -> str:
    return f"{name}'s own company knowledge: its people, its systems and how it works."


def skill_description(name: str) -> str:
    return (
        f"What is true of {name} as a company: who works there and in which role, which systems it uses "
        f"and where, and what it has agreed about how it does its business. Use for any work done for "
        f"{name} or any question about {name}, together with the skill of the tool the work happens in."
    )


def render(root: Path, name: str, slug: str) -> Rendering:
    if not SLUG.match(slug):
        raise RenderError(f"slug '{slug}' must be lower-case kebab-case")
    master, required = read_master(root)
    documents = read_documents(root)

    findings = check_self_contained((root / MASTER).read_text(encoding="utf-8"), MASTER, root, PurePosixPath())
    for document in documents.values():
        findings += check_self_contained(document.body, str(document.path), root, document.path.parent)
        for _, target in LINK.findall(document.body):
            linked = documents.get(resolve(document.path.parent, target) or PurePosixPath())
            if document.visibility is Visibility.PUBLIC and linked and linked.visibility is Visibility.INTERNAL:
                findings.append(f"{document.path}: a public document may not depend on the internal {linked.path}")
    for path in required:
        if path not in documents:
            findings.append(f"{MASTER}: required read {path} is not a document under {DOCS}/")
    if len(set(required)) != len(required):
        findings.append(f"{MASTER}: a required read is listed twice")
    if findings:
        raise RenderError("\n".join(findings))

    inline = [documents[path] for path in required]
    referenced = [document for path, document in documents.items() if path not in set(required)]
    fixed = passage(name, slug)

    def link_rewriter(document: Document, in_skill: bool) -> Callable[[re.Match[str]], str]:
        def rewrite(match: re.Match[str]) -> str:
            text, target = match.group(1), match.group(2)
            resolved = resolve(document.path.parent, target)
            linked = documents.get(resolved) if resolved else None
            if linked is None:
                return match.group(0)
            if in_skill:
                if linked.path in set(required):
                    return f"{text} (in this skill)"
                return f"[{text}](references/{linked.reference})"
            return f"[{text}]({linked.path})"

        return rewrite

    def body(document: Document, in_skill: bool, levels: int) -> str:
        lines = document.body.splitlines()
        rewrite = link_rewriter(document, in_skill)
        for index, line in outside_code(lines):
            lines[index] = LINK.sub(rewrite, line)
        return demote("\n".join(lines), levels)

    header = (
        f"<!-- Generated from {MASTER} and {DOCS}/ by the company-skill workflow. "
        "Do not edit this file: change those documents. -->\n"
    )
    entry = [header, master, fixed]
    entry += [body(document, False, 1) for document in inline]
    files = {ENTRYPOINT: "\n".join(part.strip() + "\n" for part in entry)}

    plugin_dir = f"plugins/{slug}"
    skill_dir = f"{plugin_dir}/skills/{slug}"
    description = skill_description(name)
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise RenderError(f"skill description is {len(description)} characters; the limit is {MAX_DESCRIPTION_CHARS}")
    skill = [
        f"---\nname: {slug}\ndescription: {json.dumps(description)}\n---\n",
        f"# {name}\n",
        master,
        fixed,
    ]
    skill += [body(document, True, 1) for document in inline]
    if referenced:
        listing = ["## More about " + name + "\n", "Read when the work touches it.\n"]
        listing += [f"- `references/{document.reference}`: {document.description}" for document in referenced]
        skill.append("\n".join(listing) + "\n")
    files[f"{skill_dir}/SKILL.md"] = "\n".join(part.strip() + "\n" for part in skill)
    for document in referenced:
        files[f"{skill_dir}/references/{document.reference}"] = body(document, True, 0)
    return Rendering(files, plugin_dir)


def manifest(slug: str, name: str, version: str) -> str:
    content = {"name": slug, "version": version, "description": plugin_description(name), "author": {"name": name}}
    return json.dumps(content, indent=2) + "\n"


def raise_patch(version: str) -> str:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise RenderError(f"plugin version '{version}' is not MAJOR.MINOR.PATCH")
    major, minor, patch = (int(part) for part in match.groups())
    return f"{major}.{minor}.{patch + 1}"


def committed_tree(root: Path, folder: str) -> dict[str, str]:
    base = root / folder
    if not base.is_dir():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(base.rglob("*"))
        if path.is_file()
    }


def catalog_with_entry(root: Path, slug: str, name: str) -> str:
    path = root / CATALOG
    if not path.is_file():
        raise RenderError(f"{CATALOG} is missing: the company's repository is its plugin catalog")
    catalog = Catalog.parse(path.read_text(encoding="utf-8"))
    wanted: JsonObject = {"name": slug, "source": f"./plugins/{slug}", "description": plugin_description(name)}
    entry = catalog.entry(slug)
    if entry is None:
        catalog.plugins.append(wanted)
    else:
        entry.update(wanted)  # keeps any other field an operator set on the entry
    # The same form tools/update_catalog.py writes, so the two never rewrite each other's bytes.
    return json.dumps(catalog.document, indent=2) + "\n"


def plan(root: Path, name: str, slug: str) -> tuple[dict[str, str | None], str]:
    """Every file to write (None: to remove), and the plugin version the result carries."""
    rendering = render(root, name, slug)
    plugin_json = f"{rendering.plugin_dir}/.claude-plugin/plugin.json"
    committed = committed_tree(root, rendering.plugin_dir)
    previous = manifest_version(committed[plugin_json]) if plugin_json in committed else None
    version = previous or FIRST_VERSION
    plugin_files = {path: text for path, text in rendering.files.items() if path.startswith(rendering.plugin_dir + "/")}
    plugin_files[plugin_json] = manifest(slug, name, version)
    if previous is not None and plugin_files != committed:
        version = raise_patch(previous)
        plugin_files[plugin_json] = manifest(slug, name, version)

    wanted: dict[str, str | None] = {path: None for path in committed}
    wanted.update(plugin_files)
    wanted[ENTRYPOINT] = rendering.files[ENTRYPOINT]
    wanted[CATALOG] = catalog_with_entry(root, slug, name)
    return wanted, version


def resolve_slug(slug: str, repository: str) -> str:
    """The slug given, or else the <slug> of a repository named owner/company-<slug>."""
    if slug:
        return slug
    name = repository.rpartition("/")[2]
    if not name.startswith("company-"):
        raise RenderError(f"repository '{repository}' is not named company-<slug>; pass --slug")
    return name.removeprefix("company-")


def changed_files(root: Path, wanted: dict[str, str | None]) -> list[str]:
    """The files whose content on disk differs from the rendering, in path order."""
    changed = []
    for relative, content in sorted(wanted.items()):
        path = root / relative
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current != content:
            changed.append(relative)
    return changed


def write(root: Path, wanted: dict[str, str | None], changed: list[str]) -> None:
    for relative in changed:
        path = root / relative
        content = wanted[relative]
        if content is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def render_into(root: Path, name: str, slug: str) -> tuple[list[str], str]:
    """Write the rendering into the checkout at root; the files changed and the plugin version."""
    wanted, version = plan(root, name, slug)
    changed = changed_files(root, wanted)
    write(root, wanted, changed)
    return changed, version


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--name", required=True, help="the company's name as people write it")
    parser.add_argument("--slug", default="", help="the <slug> of the repository name company-<slug>")
    parser.add_argument("--repository", default="", help="owner/company-<slug>, read when --slug is empty")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        slug = resolve_slug(args.slug, args.repository)
        wanted, version = plan(root, args.name, slug)
    except (RenderError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.validate:
        print(f"the documents of {slug} render without findings (plugin {version} if published now)")
        return 0
    changed = changed_files(root, wanted)
    if args.check:
        if changed:
            print("the committed rendering differs from the documents:", file=sys.stderr)
            for relative in changed:
                print(f"  {relative}", file=sys.stderr)
            return 1
        print(f"rendering of {slug} is current (plugin {version})")
        return 0
    write(root, wanted, changed)
    print(f"rendered {slug}: plugin {version}, {len(changed)} file(s) changed")
    for relative in changed:
        print(f"  {relative}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
