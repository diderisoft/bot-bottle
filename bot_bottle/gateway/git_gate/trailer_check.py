"""Check every commit a push introduces carries this bottle's `Bot-Bottle`
trailer (PRD 0112).

    python3 -m bot_bottle.gateway.git_gate.trailer_check <refs-file>

Run from the bare repo by pre-receive. The repo's `git-gate.trailers` is the
mode (`require`, `warn`, `off`; unset is `off`, for repos provisioned before
the check existed) and `git-gate.trailer` the expected value. A commit is new
when no ref the gate already has reaches it (`<new> --not --all`), so commits
already upstream are never checked; merge commits made in the bottle are.
A commit passes when it has a `Bot-Bottle` trailer and every one it has names
this bottle and run. The check rejects, it never rewrites.

Exit 0 to accept (clean, `warn`, or `off`), 1 to refuse.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bot_bottle.contract.git_trailers import (
    TRAILER_KEY, TRAILER_MODES, TRAILER_OFF, TRAILER_WARN,
)

ACCEPT = 0
REFUSE = 1

_ZERO = "0" * 40
# How many offending commits the message lists.
_LIST_LIMIT = 10

Git = Callable[..., str]


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, errors="replace", check=True,
    ).stdout


def _config(git: Git, key: str) -> str:
    try:
        return git("config", "--get", key).strip()
    except subprocess.CalledProcessError:
        return ""


@dataclass(frozen=True)
class Offender:
    sha: str
    subject: str
    found: tuple[str, ...]  # the commit's Bot-Bottle values; () when missing

    def describe(self) -> str:
        why = ("no " + TRAILER_KEY + " trailer" if not self.found
               else "names " + ", ".join(self.found))
        return f"{self.sha[:12]} {self.subject[:60]}  ({why})"


def offenders(git: Git, new: str, expected: str) -> list[Offender]:
    """Commits reachable from `new` and no ref the gate has whose trailers
    don't name `expected`."""
    # Unit separator between fields, record separator between commits.
    fmt = (f"%H%x1f%s%x1f%(trailers:key={TRAILER_KEY},valueonly,"
           "separator=%x1d)%x1e")
    out = git("log", f"--format={fmt}", new, "--not", "--all")
    bad: list[Offender] = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, subject, values = (record.split("\x1f") + ["", ""])[:3]
        found = tuple(v.strip() for v in values.split("\x1d") if v.strip())
        if not found or any(v != expected for v in found):
            bad.append(Offender(sha=sha, subject=subject, found=found))
    return bad


def rebase_base(git: Git, new: str) -> str:
    """What to rebase onto to amend the push's new commits: the one commit
    already on the gate they start from, `--root` when they reach none, else
    a placeholder."""
    try:
        out = git("rev-list", "--boundary", new, "--not", "--all")
    except subprocess.CalledProcessError:
        return "<upstream branch>"
    boundary = [line[1:] for line in out.splitlines() if line.startswith("-")]
    if len(boundary) == 1:
        return boundary[0][:12]
    if not boundary:
        return "--root"
    return "<upstream branch, e.g. origin/main>"


def fix_command(expected: str, base: str) -> str:
    amend = (f"git -c trailer.ifexists=replace commit --amend --no-edit --allow-empty "
             f"--trailer \"{TRAILER_KEY}: {expected}\"")
    return f"git rebase --rebase-merges --exec '{amend}' {base}"


def check(refs: list[tuple[str, str, str]], *, git: Git = _git,
          err: Callable[[str], None] = lambda s: print(s, file=sys.stderr)) -> int:
    mode = _config(git, "git-gate.trailers") or TRAILER_OFF
    if mode not in TRAILER_MODES:
        err(f"git-gate: unknown git-gate.trailers {mode!r}; refusing push")
        return REFUSE
    if mode == TRAILER_OFF:
        return ACCEPT
    expected = _config(git, "git-gate.trailer")
    if not expected:
        err("git-gate: this repo has no git-gate.trailer to check; refusing push")
        return REFUSE
    failed = False
    for _old, new, ref in refs:
        if new == _ZERO:
            continue
        try:
            bad = offenders(git, new, expected)
        except subprocess.CalledProcessError as e:
            err(f"git-gate: could not read the commits pushed to {ref}: "
                f"{(e.stderr or '').strip()}")
            return REFUSE
        if not bad:
            continue
        failed = True
        verdict = "warning" if mode == TRAILER_WARN else "refusing"
        err(f"git-gate: {verdict} {ref}: {len(bad)} new commit(s) don't carry "
            f"this bottle's trailer")
        err(f"git-gate:   {TRAILER_KEY}: {expected}")
        for o in bad[:_LIST_LIMIT]:
            err(f"git-gate:   {o.describe()}")
        if len(bad) > _LIST_LIMIT:
            err(f"git-gate:   ... and {len(bad) - _LIST_LIMIT} more")
        err("git-gate: add it to each commit (the commit hooks bot-bottle "
            "provisions do this for new commits), then push again:")
        err(f"git-gate:   {fix_command(expected, rebase_base(git, new))}")
    if failed and mode != TRAILER_WARN:
        return REFUSE
    return ACCEPT


def read_refs(path: Path) -> list[tuple[str, str, str]]:
    refs: list[tuple[str, str, str]] = []
    for line in path.read_text().splitlines():
        parts = line.split(" ")
        if len(parts) == 3 and parts[2]:
            refs.append((parts[0], parts[1], parts[2]))
    return refs


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: trailer_check <refs-file>", file=sys.stderr)
        return REFUSE
    return check(read_refs(Path(argv[1])))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
