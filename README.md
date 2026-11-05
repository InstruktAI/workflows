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
| `google/osv-scanner-action` | always; diff mode on `pull_request`, full lockfile scan otherwise | any osv.dev finding, including `MAL-` malicious-package entries (OpenSSF malicious-packages) |
| `cargo-deny` (`advisories bans sources`) | `Cargo.toml` | vulnerabilities, yanked crates, non-crates.io registries, git sources; unmaintained crates only warn. Uses the repo's `deny.toml` if present, else `deny/default.toml` from the same workflow commit. `cargo-deny-licenses: true` adds `licenses`. |
| MSRV `cargo check --workspace --all-targets --locked` | `Cargo.toml` declares `rust-version` | the workspace not building on exactly that toolchain |
| `pip-audit` over `uv export` | `pyproject.toml` (a missing `uv.lock` fails) | any known vulnerability in the locked set, all groups and extras |
| `npm audit` / `pnpm audit` | `package.json` with `package-lock.json` / `pnpm-lock.yaml` (no lockfile fails) | advisories at `node-audit-level` (default `low`) or above |

Each gate has a boolean input to switch it off: `osv-scanner`, `cargo-deny`,
`msrv`, `pip-audit`, `node-audit`.

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
check and check run on the PR is green (no branch protection needed); majors
never auto-merge; non-security updates wait `minimumReleaseAge: "3 days"`,
vulnerability fixes skip the wait and get the `security` label; lock file
maintenance is off: it would move every transitive dependency to brand-new
versions with no release age to wait for, which is the window malicious
releases use. Transitive dependencies move with their direct dependency, and
vulnerable ones get vulnerability PRs. Those need the repo's Dependabot alerts
on.

## Not here on purpose

itsUP deploys stay in `Morriz/itsUP` (`reconcile.yml`) — the deployment
target owns its plumbing. Call it directly with `secrets: inherit`.

## Notes

- Private-repo reuse requires this repo's Actions access setting
  "Accessible from repositories in the organization" (set once, already on).
- Version with tags: breaking changes bump `v1` → `v2`; callers pin the tag.
