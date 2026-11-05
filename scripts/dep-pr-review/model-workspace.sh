#!/usr/bin/env bash
# Turns the current directory into the minimal git repository
# claude-code-action needs: one empty commit (it lists tracked files against
# HEAD) and a plain `origin` remote (on pull requests it fetches the base
# branch to restore .claude/ and CLAUDE.md, src/github/operations/
# restore-config.ts). No pull-request content is checked out. The action
# rewrites `origin` with its read-only token before fetching
# (src/github/operations/git-config.ts); that token is the job's own
# GITHUB_TOKEN, which expires when the job ends, and the model's settings
# deny reads under .git/.
set -euo pipefail
repo=${1:?usage: model-workspace.sh OWNER/REPO}
git init -q -b dep-pr-review .
git -c user.name=dep-pr-review -c user.email=dep-pr-review@invalid \
  commit -q --allow-empty -m "dep-pr-review model workspace"
git remote add origin "https://github.com/${repo}.git"
