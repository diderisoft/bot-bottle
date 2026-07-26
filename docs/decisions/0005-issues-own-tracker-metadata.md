# ADR 0005: Keep tracker metadata on issues

- **Status:** Accepted
- **Date:** 2026-07-18
- **Deciders:** didericis

> **Amended 2026-07-26.** A pull request may carry labels directly instead of
> linking a tracking issue. When a PR does link an issue, the issue remains the
> canonical owner of planning metadata and the reference is validated.

## Context

Gitea exposes labels on both issues and pull requests. Applying the same labels
to both copies planning metadata, creates a synchronization obligation, and
makes disagreements between the two records possible. At the same time,
unlabelled objects look accidental unless the repository states which object
owns the metadata.

The repository already uses issues as work items and PRs as implementations of
those work items. At this decision's cutoff, all open PRs reference issues, but
121 of 219 historically merged PRs do not. Manufacturing retrospective issues
for that history would create records that never participated in planning and
would make the issue history less truthful.

## Decision

Issues are the canonical tracker records and own labels when a separate work
item exists. Every issue has at least one label. An issue opened or left
without labels receives `Status/Needs Triage` automatically until it is
classified.

Every new pull request is tracked in exactly one of two mutually exclusive
ways:

1. It deliberately references at least one existing issue in its title or
   description. Tracker metadata stays on that issue and the PR remains
   unlabelled.
2. It carries at least one label directly when a separate issue would add no
   useful planning context.

Issue references use one of these forms:

- `Closes #123`, `Fixes #123`, or `Resolves #123` when merging completes it.
- `Part of #123`, `Related to #123`, `Refs #123`, or `References #123` when it
  contributes without completing it.

Gitea Actions enforces the exclusive either/or PR rule, validates any issue
references, and repairs the empty issue-label state. Branch protection makes
the PR policy check required.

The policy applies from 2026-07-18 onward. Existing issues may be labelled as
they are encountered, but closed PRs are grandfathered: no retrospective
issues or PR labels are created solely to make history conform.

## Consequences

- Classification, priority, and workflow metadata have one source of truth for
  each change: the linked issue when one exists, otherwise the PR.
- For issue-backed changes, the PR's issue link is the navigation path to its
  planning metadata.
- Multi-PR issues do not require copied or synchronized labels.
- Small standalone changes do not require a tracking issue created solely to
  satisfy automation.
- `Status/Needs Triage` is an intentional fallback, not a final
  classification.
- Direct issue creation remains convenient; automation repairs a missing label
  immediately after creation because Gitea has no native required-label rule.
- The required check must be configured in branch protection after this
  workflow lands.

## Links

- Issue #405.
- `.gitea/workflows/tracker-policy-pr.yml`.
- `.gitea/workflows/tracker-policy-issues.yml`.
- `scripts/tracker_policy.py`.
