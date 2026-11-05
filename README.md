# InstruktAI workflows

Reusable GitHub Actions pipelines, consumed by version tag:

```yaml
jobs:
  ci:
    uses: InstruktAI/workflows/.github/workflows/ci-node.yml@v1
```

Every pipeline is thin by design: checkout → toolchain → the repo's `make`
buttons (`lint`, `test`, `e2e`) → optional deploy. The Makefile button
contract lives in the TeleClaude docs
(`sdlc/policy/ci-pipeline-conventions`); repos own their
toolchain, pipelines just push buttons.

## Catalog

| Workflow               | Purpose                                          |
| ---------------------- | ------------------------------------------------ |
| `ci-node.yml`          | Node/TS repos: install, `make lint test`, `make e2e` when present |
| `ci-python.yml`        | Python repos: uv install, `make lint test`, `make e2e` when present |
| `ci-rust.yml`          | Rust repos: toolchain, `make lint test`, `make e2e` when present |
| `deploy-cloudflare.yml`| Build and deploy `dist/` to a Cloudflare Pages project |
| `deps-audit.yml`       | Dependency safety gates, auto-detected per ecosystem (see below) |
| `dep-pr-review.yml`    | Provenance and AI code review of each dependency a Renovate PR updates (see below) |

This repo's own workflows, not for callers: `release.yml` (releases this
repo, see "Releasing"), `actionlint.yml`, `self-test.yml`,
`self-test-dep-pr-review.yml`.

Shared config, not workflows:

| File                   | Purpose                                          |
| ---------------------- | ------------------------------------------------ |
| `renovate/default.json`| Renovate preset: green non-major updates auto-merge (see below) |
| `deny/default.toml`    | cargo-deny baseline `deps-audit.yml` uses when a repo ships no `deny.toml` |

## Dependency safety: `deps-audit.yml`

Every gate is a maintained third-party tool reading its own advisory
database; the workflow only detects ecosystems in `working-directory` and
wires the tools up. It needs `contents: read` and no secrets.

| Gate | Runs when | Fails on |
| ---- | --------- | -------- |
| `actions/dependency-review-action` | `pull_request`, public repo (private repos need GitHub Advanced Security; `dependency-review: always` forces it, `never` skips it) | a dependency added or changed with an advisory at `fail-on-severity` (default `low`) or above |
| `google/osv-scanner-action` | always: a full scan of every lockfile in the head commit (on `pull_request` also an informational report of what the PR introduces) | any osv.dev finding, including `MAL-` malicious-package entries (OpenSSF malicious-packages), whether or not the change introduced it |
| `cargo-deny` (`advisories bans sources`) | `Cargo.toml` | vulnerabilities, yanked crates, non-crates.io registries, git sources; unmaintained crates only warn. Uses the repo's `deny.toml` if present, else `deny/default.toml` from the same workflow commit. `cargo-deny-licenses: true` adds `licenses`. |
| MSRV `cargo check --all-targets --locked` | any workspace member declares `rust-version` (read from `cargo metadata`, so inherited and virtual workspaces count) | the code not building on exactly that toolchain: one check per distinct rust-version, `--workspace` when every member shares it, else `-p` for the members that declare it |
| `pip-audit` over `uv export` | `pyproject.toml` (a missing `uv.lock` fails) | any known vulnerability in the locked set: every workspace member (`--all-packages`), all groups and extras; `--strict`, so a locked package PyPI no longer serves (typical for removed malicious ones) fails instead of being skipped |
| `npm audit` / `pnpm audit` | `package.json` with `package-lock.json` / `pnpm-lock.yaml` (no lockfile fails) | advisories at `node-audit-level` (default `low`) or above |

Each gate has a boolean input to switch it off: `osv-scanner`, `cargo-deny`,
`msrv`, `pip-audit`, `node-audit`.

`pip-audit` only knows PyPI. Workspace members and local path dependencies are
not exported, so they are never looked up; but a locked package that comes
from a private index, and so is unknown to PyPI, fails under `--strict` the
same way a removed malicious package does. That is intentional: a package the
gate cannot audit is never passed as clean. A repo that depends on a private
index sets `pip-audit: false` and keeps the `osv-scanner` gate, which audits the
same lockfile against osv.dev.

```yaml
on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

jobs:
  deps:
    uses: InstruktAI/workflows/.github/workflows/deps-audit.yml@v1
    # with:
    #   working-directory: crates/app
    #   node-audit-level: moderate
```

`self-test.yml` runs it against the fixtures in `test/fixtures/` on every
change to this repo.

## Renovate preset

```json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": ["github>InstruktAI/workflows//renovate/default#v1"]
}
```

What it sets: `config:recommended`, `:semanticCommits` and
`helpers:pinGitHubActionDigestsToSemver`, so every action is pinned to a full
commit SHA with its version as a comment (`@<sha> # v1.2.3`); minor, patch, pin
and digest updates auto-merge with `automergeType: "pr"` and
`platformAutomerge: false`, so Renovate merges itself only once every status
check and check run present on the PR is green (see the limit below); majors
never auto-merge; non-security updates wait `minimumReleaseAge: "3 days"`,
vulnerability fixes skip the wait and get the `security` label; lock file
maintenance is off: it would move every transitive dependency to brand-new
versions with no release age to wait for, which is the window malicious
releases use. Transitive dependencies move with their direct dependency, and
vulnerable ones get vulnerability PRs. Those need the repo's Dependabot alerts
on.

Add `dep-pr-review.yml` to the repo's own workflows (below): its check is one
of the checks Renovate waits for, so a dependency PR merges only after its
provenance and code change were reviewed.

### Checks Renovate cannot wait for

Renovate's wait covers only the checks that already exist when it evaluates
the branch: it reads the commit statuses and check runs on the head and
merges when all of them are green (`getBranchStatus` in
`lib/modules/platform/github/index.ts`, Renovate 44.219.0). It has no setting
that names a check it must wait for. The repo's own workflow runs start on
the push, and Renovate only tries to automerge on a later run, so their jobs
are in place by then. A check posted by a third-party app (Socket, for
example) can arrive after Renovate has evaluated, and the PR merges without
it. Requiring a check also covers your own jobs if you want that guarantee.

Make every such check a required status check with a repository ruleset that
no one bypasses, Renovate included. GitHub then refuses the merge until the
check has passed. Find the exact check name and app id on a recent PR:

```sh
gh api repos/OWNER/REPO/commits/SHA/check-runs \
  --jq '.check_runs[] | "\(.name)  app=\(.app.id)"'
```

Then create the ruleset (repo admin):

```sh
gh api -X POST repos/OWNER/REPO/rulesets --input - <<'JSON'
{
  "name": "required third-party checks",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [],
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "rules": [
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "required_status_checks": [
          { "context": "CHECK NAME", "integration_id": APP_ID }
        ]
      }
    }
  ]
}
JSON
```

`integration_id` ties the check to that app, so no other app or workflow can
satisfy it by reusing the name. A required check also applies to direct pushes
to the default branch: if release automation pushes there, give only that
identity a bypass, never Renovate.

## Dependency PR review: `dep-pr-review.yml`

For a pull request opened by `renovate[bot]` (others only with
`review-all-authors: true`), every updated GitHub Action, crate, PyPI package
and npm package gets two answers:

- **Provenance**, decided by scripts (`scripts/dep-pr-review/collect.py`).
  It fails the check outright when the new version is not the upstream
  project's official release:
  - action: the pinned SHA is the commit of the upstream tag named in its
    version comment, and a GitHub release exists for that tag;
  - crate: the `.crate` matches its crates.io checksum, declares the same
    `repository` as the previous version, and its `.cargo_vcs_info.json`
    commit is the upstream release tag. A workspace crate without a tag of
    its own passes only when that commit is contained in a release tag
    verified in the same PR for a sibling crate of the same repository;
  - npm: the npm registry signature verifies; the declared repository is the
    one the previous version came from; a SLSA provenance attestation, when
    present, names that repository and the tarball's digest and was built
    from the upstream release tag of exactly this version at the tagged
    commit (a branch build fails, and so does dropping provenance); without
    one, the upstream release tag must exist;
  - PyPI: every distribution matches its PyPI digest; the source repository
    and trusted publisher are the previous version's; a PEP 740 attestation's
    signing certificate names the release tag of exactly this version at the
    tagged commit (a branch build fails, and so does dropping attestations or
    attesting only some files); without one, the upstream release tag must
    exist.

  Identity is anchored on the previous version's publication, never on the
  new one's own metadata. A new dependency has nothing to anchor on and
  blocks for a human. Sigstore bundles are not verified cryptographically
  here; the registries verify them on upload.

  Where a source cannot be tied to a release, the result is `unverifiable`,
  which blocks the PR with its reason just as `fail` does: only positive
  provenance for every update lets it pass.
  The PR's base and head trees are written from git objects (`git ls-tree`
  and `git cat-file`), never with `git archive`, whose `export-ignore` and
  `export-subst` attributes a PR could use to hide a lockfile; the written
  paths must equal `git ls-tree`. A changed submodule pointer blocks.
- **Sanity**, decided by the model: the diff between the old and new
  *published artifacts* (not the lockfile) must match the release notes. New
  network calls, secret, credential or environment access, install or build
  scripts, and encoded or obfuscated blobs are flagged to it by pattern.

The model runs in its own job with a read-only token and no pull-request
checkout. Its working directory holds only `prompt.md`, the bundle and an
empty git repository, which the action requires (it fetches the base branch
on pull requests); the action stores this job's read-only `GITHUB_TOKEN` in
that repository's remote, so reads under `.git/` are denied, a verdict that
contains the token is discarded, and the token expires when the job ends,
before anything is posted. `--tools
"Read,Glob,Grep"` leaves it no other built-in tool, `--disallowedTools
"mcp__*"` no MCP tool, and the `blockReadsOutsideWorkingDirectories` setting
makes its file tools refuse anything outside that directory, such as
`/proc` or `$HOME`. `self-test-dep-pr-review.yml`'s `sandbox-probe` asks the
real model to read a canary outside and checks the canary never reaches the
transcript. Its JSON is validated with jq.

What remains: everything inside the bundle is attacker-controllable text the
model reads (prompt injection is mitigated, not impossible), which is why
provenance is decided by scripts and a verdict only ever blocks or allows
together with them. The read boundary is Claude Code's own enforcement;
its docs call path rules best-effort for Grep and Glob. A separate job posts the outcome as a PR comment
and fails the check on a provenance failure, a `not_sane` verdict, any
dependency file it cannot review, or any missing result.

```yaml
on:
  pull_request:

permissions:
  contents: read

jobs:
  dep-review:
    permissions:
      contents: read
      pull-requests: write
    uses: InstruktAI/workflows/.github/workflows/dep-pr-review.yml@v1
    secrets:
      claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

Known limits: minified or bundled code (an action's `dist/`, a minified npm
build) cannot be reviewed reliably; for ordinary updates the 3-day release age
is the backstop, and advisory fixes skip it. Actions pinned to a branch rather
than a release (`# master`, `# stable`) fail provenance and wait for a human.
What is reviewed: GitHub Actions `uses:` in workflow and `action.yml` files
(block or flow style), crates.io entries of `Cargo.lock`, PyPI entries of
`uv.lock`, and default-registry entries of npm v2/v3 `package-lock.json` and
pnpm 9 `pnpm-lock.yaml`. Every other added or changed entry in those files
is blocked by name as unhandled: Cargo git sources and alternate registries,
uv git, URL and non-PyPI registry sources, npm entries resolved anywhere but
`registry.npmjs.org`, npm v1 lockfiles, other pnpm lockfile versions and
resolutions, `docker://` actions. Changes to `go.sum`, `yarn.lock`,
`poetry.lock`, `Pipfile.lock`, `requirements*.txt`, Dockerfiles and toolchain
pins (`.nvmrc`, `rust-toolchain`, ...) block too. Repository-local entries
(path, editable, workspace members, `./` actions) are part of the PR itself
and are not treated as dependencies.

## Releasing this repository

`release.yml` (Actions → release → Run workflow on `main`, `version` vX.Y.Z)
creates the immutable tag vX.Y.Z and moves the floating major tag vX to it in
one atomic push, then publishes the GitHub Release.

A tag ruleset (`.github/rulesets/release-tags.json`) blocks creating,
updating and deleting `refs/tags/v*` for everyone but its bypass actor. A
ruleset cannot exempt a single workflow: its bypass actors are roles, teams,
apps, deploy keys and users
(docs.github.com/en/rest/repos/rules), and exempting GitHub Actions' token
would exempt every workflow in the repo, including one pushed on a branch.
The bypass is therefore a deploy key whose private half is a secret of the
`release` environment, which admits `main` only, so only `release.yml` on main
can push tags. One-time setup (repo admin):

```sh
ssh-keygen -t ed25519 -N '' -C 'InstruktAI/workflows release' -f release_key
gh repo deploy-key add release_key.pub -R InstruktAI/workflows --allow-write --title release
gh api -X PUT repos/InstruktAI/workflows/environments/release \
  -F 'deployment_branch_policy[protected_branches]=false' \
  -F 'deployment_branch_policy[custom_branch_policies]=true'
gh api -X POST repos/InstruktAI/workflows/environments/release/deployment-branch-policies \
  -f name=main -f type=branch
gh secret set RELEASE_DEPLOY_KEY --env release -R InstruktAI/workflows < release_key
rm release_key release_key.pub
gh api -X POST repos/InstruktAI/workflows/rulesets --input .github/rulesets/release-tags.json
```

The `DeployKey` bypass covers every deploy key of the repo, so keep the
release key the only one with write access. Repo admins can still edit or
disable the ruleset.

## Not here on purpose

itsUP deploys stay in `Morriz/itsUP` (`reconcile.yml`) — the deployment
target owns its plumbing. Call it directly with `secrets: inherit`.

## Notes

- Private-repo reuse requires this repo's Actions access setting
  "Accessible from repositories in the organization" (set once, already on).
- Version with tags: breaking changes bump `v1` → `v2`. InstruktAI repos
  consume a released tag (`@v1`), per `sdlc/policy/ci-pipeline-conventions`.
  Other organisations should pin the commit SHA of a release (`@<sha> #
  v1.2.3`): every action this repo calls is SHA-pinned too, so that pin holds
  all the way down.
