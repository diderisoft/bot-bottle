"""The pre-receive phase that protects a repo's default branch (#808).

A bottle with `git-gate.protect_default_branch: true` gets
`git-gate.protectDefault true` on each of its gate repos. The phase then
refuses a push that deletes, or rewrites (non-fast-forward), the upstream's
default branch. Force pushes to other branches still go through: agents
rebase their own branches.

The default branch is the upstream's HEAD, asked of the upstream at push
time (`ls-remote --symref`), since the gate's copy of HEAD is only synced on a
repo's first fetch. When the upstream doesn't say, the push is refused: a
protected repo fails closed. Only a delete or a rewrite of a branch asks.

It runs inside pre-receive after `upstream_git` (the credentialed git) is
defined and before any ref is forwarded, reading the saved stdin in
`$refs_file`. `ls-remote` gets `/dev/null` as stdin, so ssh can't swallow the
ref list the loop is reading."""

from __future__ import annotations

PROTECT_DEFAULT_BRANCH_PHASE = r"""# Phase 2a: with git-gate.protectDefault (the bottle's
# git-gate.protect_default_branch), refuse deleting or rewriting the
# upstream's default branch. Fails closed when the upstream won't say which
# branch that is.
if [ "$(git config --bool git-gate.protectDefault 2>/dev/null)" = "true" ]; then
  default_ref=
  while IFS=' ' read -r old new ref; do
    case "$ref" in refs/heads/*) ;; *) continue ;; esac
    if [ "$new" = "$zero" ]; then
      change="delete"
    elif [ "$old" != "$zero" ] && ! git merge-base --is-ancestor "$old" "$new" 2>/dev/null; then
      change="force-push to"
    else
      continue
    fi
    if [ -z "$default_ref" ]; then
      default_ref=$(upstream_git ls-remote --symref "$(git config --get remote.origin.url)" HEAD \
        </dev/null 2>/dev/null | awk '/^ref:/ {print $2; exit}')
    fi
    if [ -z "$default_ref" ]; then
      echo "git-gate: refusing to $change $ref: this bottle protects the default branch, and the upstream didn't say which branch that is" >&2
      audit_push forge.push_rejected blocked "$ref" --detail "default branch unknown (protect_default_branch)"
      exit 1
    fi
    if [ "$ref" = "$default_ref" ]; then
      echo "git-gate: refusing to $change $ref: it is the upstream's default branch, which this bottle protects (git-gate.protect_default_branch)" >&2
      echo "git-gate: push your work to another branch and open a pull request instead" >&2
      audit_push forge.push_rejected blocked "$ref" --detail "default branch protected"
      exit 1
    fi
  done < "$refs_file"
fi
"""

__all__ = ["PROTECT_DEFAULT_BRANCH_PHASE"]
