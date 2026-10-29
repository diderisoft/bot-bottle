# ADR 0001: Merge PRs with rebase, not merge commits

- **Status:** Accepted
- **Date:** 2026-05-28
- **Deciders:** didericis

## Context

PRs need a merge strategy. Gitea offers merge-commit, squash, rebase,
and rebase-merge. The project uses [Conventional
Commits](https://www.conventionalcommits.org/) enforced by a
`commit-msg` hook, and PRDs typically land as a multi-commit PR where
each commit is meaningful on its own (e.g. PR #95: a `docs(prd)` commit,
a `feat(manifest)` implementation commit, and a `docs(manifest)`
commit). The history should stay readable and the individual
conventional commits should survive onto `main`.

## Decision

Merge PRs with **rebase** (Gitea's `rebase` style; `Do: "rebase"` via
the API). The branch's commits are replayed onto `main` with no merge
commit, producing a linear history that preserves each commit verbatim.

## Consequences

- **Linear history**, no merge bubbles; `git log --oneline` reads as a
  straight sequence of conventional commits.
- **Each commit is preserved** (unlike squash, which would collapse the
  PRD/impl/docs commits into one and lose the staged structure).
- **Commit SHAs are rewritten at merge.** The replayed commits on `main`
  get new SHAs, and the source branch is deleted, so a link to a file
  by *branch name* (`/src/branch/<feature>/…`) dies at merge. This is
  why links to not-yet-merged files are pinned to a **commit SHA**
  (`/src/commit/<sha>/…`), which stays reachable via the retained
  `refs/pull/<n>/head` ref. See the research note
  *issue-tracking-vs-in-repo-decision-history* (design repo).
- **Trade-off accepted:** without a merge commit, the "these commits
  landed together as PR #N" grouping is not recorded in git itself — it
  lives in forge state (the PR). That is a mild concession against the
  keep-history-in-the-repo posture; the conventional-commit scopes and
  PRD references in the messages keep changes traceable without it.

## Links

- Research note *issue-tracking-vs-in-repo-decision-history* (design
  repo) — the commit-pinning consequence above.
- Observed practice: PRs #92, #93 merged with rebase; #95 to follow.
