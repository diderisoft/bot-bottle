"""Check every commit a push introduces carries this bottle's `Bot-Bottle`
trailer (PRD 0112).

    python3 -m bot_bottle.gateway.git_gate.trailer_check <refs-file>

Run from the bare repo by pre-receive. The repo's `git-gate.trailers` is the
mode (`require`, `warn`, `off`), `git-gate.trailer` the expected value and
`git-gate.baseline` a file of SHAs: the commits the copied workspace had at
launch that its remotes didn't (the operator's own unpushed work, which
pushes untouched; empty without one). Provisioning sets all three; a repo
missing any of them is misconfigured, and every push to it is refused. A
commit is checked when neither a ref the gate already has (so it's upstream)
nor the baseline reaches it; merge commits made in the bottle are.
A commit passes when it has a `Bot-Bottle` trailer and every one it has names
this bottle and run. The check rejects, it never rewrites.

Exit 0 to accept (clean, `warn`, or `off`), 1 to refuse, 2 for
infrastructure failure or misconfiguration.
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
MISCONFIGURED = 2  # infrastructure failure or missing config; logged separately

_ZERO = "0" * 40
# How many offending commits the message lists.
_LIST_LIMIT = 10

Git = Callable[..., str]


def _git(*args: str, stdin: str = "") -> str:
    return subprocess.run(
        ["git", *args], input=stdin, capture_output=True, text=True, errors="replace",
        check=True,
    ).stdout


_HEX = frozenset("0123456789abcdef")


def read_baseline(path: str) -> list[str] | None:
    """The baseline file's SHAs; None when it can't be read."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    shas = (line.strip() for line in text.splitlines())
    return [s for s in shas if len(s) in (40, 64) and set(s) <= _HEX]


def exclusions(git: Git, baseline: list[str]) -> str:
    """`--stdin` input excluding every ref the gate has and the baseline (one
    `^<sha>` per line). Baseline commits the gate never received are skipped
    by `--ignore-missing`."""
    tips = git("for-each-ref", "--format=%(objectname)").split()
    return "".join(f"^{sha}\n" for sha in [*tips, *baseline])


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


def offenders(git: Git, new: str, expected: str, excluded: str | None = None) -> list[Offender]:
    """Commits reachable from `new` and nothing `excluded` (`exclusions`;
    default the gate's refs) names whose trailers don't name `expected`."""
    if excluded is None:
        excluded = exclusions(git, [])
    # Unit separator between fields, record separator between commits.
    fmt = (f"%H%x1f%s%x1f%(trailers:key={TRAILER_KEY},valueonly,"
           "separator=%x1d)%x1e")
    out = git("log", f"--format={fmt}", "--ignore-missing", "--stdin", new,
              stdin=excluded)
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


def rebase_base(git: Git, new: str, excluded: str = "") -> str:
    """What to rebase onto to amend the checked commits: the one commit
    (upstream or baseline) they start from, `--root` when they reach none,
    else a placeholder."""
    try:
        out = git("rev-list", "--boundary", "--ignore-missing", "--stdin", new,
                  stdin=excluded)
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


_MISCONFIGURED_MSG = ("git-gate: {what} is missing or invalid; provisioning may have "
                      "failed, or this bottle predates commit trailers. Relaunch the "
                      "bottle to reprovision it.")


def _settings(git: Git, err: Callable[[str], None]) -> tuple[str, str, list[str]] | None:
    """(mode, expected value, baseline), or None after saying what's
    misconfigured."""
    mode = _config(git, "git-gate.trailers")
    expected = _config(git, "git-gate.trailer")
    baseline = read_baseline(_config(git, "git-gate.baseline"))
    for what, ok in (("git-gate.trailers", mode in TRAILER_MODES),
                     ("git-gate.trailer", bool(expected)),
                     ("git-gate.baseline", baseline is not None)):
        if not ok:
            err(_MISCONFIGURED_MSG.format(what=what))
            return None
    assert baseline is not None
    return mode, expected, baseline


def check(refs: list[tuple[str, str, str]], *, git: Git = _git,
          err: Callable[[str], None] = lambda s: print(s, file=sys.stderr)) -> int:
    settings = _settings(git, err)
    if settings is None:
        return MISCONFIGURED
    mode, expected, baseline = settings
    if mode == TRAILER_OFF:
        return ACCEPT
    try:
        excluded = exclusions(git, baseline)
    except subprocess.CalledProcessError as e:
        err(f"git-gate: could not list the gate's refs: {(e.stderr or '').strip()}")
        return MISCONFIGURED
    failed = False
    for _old, new, ref in refs:
        if new == _ZERO:
            continue
        try:
            bad = offenders(git, new, expected, excluded)
        except subprocess.CalledProcessError as e:
            err(f"git-gate: could not read the commits pushed to {ref}: "
                f"{(e.stderr or '').strip()}")
            return MISCONFIGURED
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
        err(f"git-gate:   {fix_command(expected, rebase_base(git, new, excluded))}")
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
