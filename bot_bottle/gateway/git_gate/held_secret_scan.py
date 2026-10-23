"""Refuse a push that carries a secret the pushing bottle holds.

gitleaks only recognises secrets by shape, so a value it has no rule for (an
arbitrary API key, a hex blob, the exfil canary) would leave in a commit even
though egress blocks the same value. This scan closes that gap. The
pre-receive hook runs it after gitleaks: it resolves the pushing bottle's
registered secrets from the orchestrator (`contract.held_secrets`) and looks
for them, with the egress DLP's known-secret matcher, in everything the push
introduces.

What a push introduces, per ref: the new ref's name, the messages and author
and committer identities of its new commits, the paths and added lines of
their diffs (binary files included, as text), and the body of an annotated
tag. Removed and context lines are already on the gate, so they aren't
scanned.

Run as `python3 -m bot_bottle.gateway.git_gate.held_secret_scan <refs-file>`
from the repository, with the pushing bottle's attribution in the
environment. Exit codes: 0 = clean, 1 = a held secret was found (refuse),
2 = the scan could not run (refuse: fail closed).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

from bot_bottle.contract.held_secrets import PUSH_SCAN_PREFIXES
from bot_bottle.gateway.egress.dlp_detectors import scan_known_secrets
from bot_bottle.gateway.policy_resolver import PolicyResolveError, PolicyResolver

CLEAN = 0
FOUND = 1
CANNOT_SCAN = 2

_ZERO = "0" * 40


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, errors="replace", check=True,
    ).stdout


def pushed_text(refs: list[tuple[str, str, str]]) -> Iterator[tuple[str, str]]:
    """(where, text) for everything the push introduces. `refs` holds the
    pre-receive `(old, new, ref)` lines."""
    for _old, new, ref in refs:
        yield f"ref name {ref}", ref
        if new == _ZERO:
            continue  # a deletion introduces nothing
        if _git("cat-file", "-t", new).strip() == "tag":
            yield f"tag {ref}", _git("cat-file", "-p", new)
        log_opts = [new, "--not", "--all"]
        yield (f"commit messages and identities on {ref}",
               _git("log", "--format=%an%n%ae%n%cn%n%ce%n%B", *log_opts))
        diff = _git("log", "-p", "--text", "--no-color", "--format=", *log_opts)
        added = [
            line for line in diff.splitlines()
            if line.startswith("+") or line.startswith("diff --git ")
            or line.startswith(("rename to ", "copy to "))
        ]
        yield f"added lines on {ref}", "\n".join(added)


def find_held_secret(
    secrets: Mapping[str, str], texts: Iterator[tuple[str, str]],
) -> str | None:
    """A description of the first held secret found, naming its key and where
    it was — never its value — or None."""
    for where, text in texts:
        result = scan_known_secrets(
            text, location=where, env=secrets, sensitive_prefixes=PUSH_SCAN_PREFIXES)
        if result is not None:
            return result.reason
    return None


def _resolve_secrets(environ: Mapping[str, str]) -> dict[str, str] | None:
    """The pushing bottle's registered secrets, or None if it can't be
    attributed or the orchestrator can't be reached."""
    source_ip = environ.get("SUPERVISE_SOURCE_IP", "")
    token = environ.get("SUPERVISE_IDENTITY_TOKEN", "")
    orch_url = environ.get("BOT_BOTTLE_ORCHESTRATOR_URL", "")
    if not source_ip or not orch_url:
        return None
    try:
        _policy, bottle_id, secrets = PolicyResolver(orch_url).resolve_policy_and_bottle_id(
            source_ip, token)
    except PolicyResolveError:
        return None
    return secrets if bottle_id else None


def _read_refs(path: Path) -> list[tuple[str, str, str]]:
    refs: list[tuple[str, str, str]] = []
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 3:
            refs.append((parts[0], parts[1], parts[2]))
    return refs


def main(
    argv: list[str],
    environ: Mapping[str, str] = os.environ,
    resolve: Callable[[Mapping[str, str]], dict[str, str] | None] = _resolve_secrets,
) -> int:
    if len(argv) != 1:
        print("git-gate: usage: held_secret_scan <refs-file>", file=sys.stderr)
        return CANNOT_SCAN
    secrets = resolve(environ)
    if secrets is None:
        print("git-gate: cannot resolve this bottle's secrets to scan the push; "
              "refusing push", file=sys.stderr)
        return CANNOT_SCAN
    try:
        found = find_held_secret(secrets, pushed_text(_read_refs(Path(argv[0]))))
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"git-gate: held-secret scan failed ({type(e).__name__}); refusing push",
              file=sys.stderr)
        return CANNOT_SCAN
    if found is not None:
        print(f"git-gate: {found}; refusing push", file=sys.stderr)
        print("git-gate: remove it from the commits (rewrite them) and push again",
              file=sys.stderr)
        return FOUND
    return CLEAN


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
