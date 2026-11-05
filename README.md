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
(`software-development/policy/ci-pipeline-conventions`); repos own their
toolchain, pipelines just push buttons.

## Catalog

| Workflow               | Purpose                                          |
| ---------------------- | ------------------------------------------------ |
| `ci-node.yml`          | Node/TS repos: install, `make lint test`, `make e2e` when present |
| `ci-python.yml`        | Python repos: uv install, `make lint test`, `make e2e` when present |
| `ci-rust.yml`          | Rust repos: toolchain, `make lint test`, `make e2e` when present |
| `deploy-cloudflare.yml`| Build and deploy `dist/` to a Cloudflare Pages project |
| `deps-audit.yml`       | Dependency safety gates, auto-detected per ecosystem (see below) |

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

What it sets: `config:recommended` and `:semanticCommits`; minor, patch, pin
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

## Not here on purpose

itsUP deploys stay in `Morriz/itsUP` (`reconcile.yml`) — the deployment
target owns its plumbing. Call it directly with `secrets: inherit`.

## Notes

- Private-repo reuse requires this repo's Actions access setting
  "Accessible from repositories in the organization" (set once, already on).
- Version with tags: breaking changes bump `v1` → `v2`; callers pin the tag.
