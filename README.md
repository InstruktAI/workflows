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

## Not here on purpose

itsUP deploys stay in `Morriz/itsUP` (`reconcile.yml`) — the deployment
target owns its plumbing. Call it directly with `secrets: inherit`.

## Notes

- Private-repo reuse requires this repo's Actions access setting
  "Accessible from repositories in the organization" (set once, already on).
- Version with tags: breaking changes bump `v1` → `v2`; callers pin the tag.
