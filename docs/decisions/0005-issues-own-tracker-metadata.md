# ADR 0005: Keep tracker metadata on issues

- **Status:** Accepted
- **Date:** 2026-07-18
- **Deciders:** didericis

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

Issues are the canonical tracker records and own labels. Every issue has at
least one label. An issue opened or left without labels receives
`Status/Needs Triage` automatically until it is classified.

Pull requests carry no labels. Every new PR deliberately references at least
one existing issue in its title or description with one of these forms:

- `Closes #123`, `Fixes #123`, or `Resolves #123` when merging completes it.
- `Part of #123`, `Related to #123`, `Refs #123`, or `References #123` when it
  contributes without completing it.

Gitea Actions enforces both PR rules as a status check and repairs the empty
issue-label state. Branch protection makes the PR policy check required.

The policy applies from 2026-07-18 onward. Existing issues may be labelled as
they are encountered, but closed PRs are grandfathered: no retrospective
issues or PR labels are created solely to make history conform.

## Consequences

- Classification, priority, and workflow metadata have one source of truth.
- A PR's issue link is the navigation path to its planning metadata.
- Multi-PR issues do not require copied or synchronized labels.
- `Status/Needs Triage` is an intentional fallback, not a final
  classification.
- Direct issue creation remains convenient; automation repairs a missing label
  immediately after creation because Gitea has no native required-label rule.
- The required check must be configured in branch protection after this
  workflow lands.

## Links

- Issue #405.
- `.gitea/workflows/tracker-policy.yml`.
- `scripts/tracker_policy.py`.
