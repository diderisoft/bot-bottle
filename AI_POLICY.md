# AI policy

Most of bot-bottle's code, docs and design notes are written by coding agents
(Claude and Codex), working under the maintainer's direction. The commit
authors and the `Co-Authored-By` trailers show which ones. This page says so
up front and describes what keeps that safe for a security tool.

## How work gets in

- **The maintainer decides what merges.** Every change goes through a pull
  request that the maintainer reviews. Agents open PRs and may merge one only
  when the maintainer has told them to.
- **Design is written down before it's built.** Features start as a PRD
  (kept in a private design repo); system-wide boundaries are decision
  records (`docs/decisions/`); how the system works today is in
  `docs/reference/`.
  Reviewers check code against those, not against a chat transcript.
- **Every PR links an issue** (ADR 0005), so each change has a stated reason.

## What CI enforces

- **Coverage, weighted by risk** (ADR 0004): the security-critical modules
  listed in `scripts/critical-modules.txt` must stay at 85% or higher, and
  new or changed lines on a branch must be at least 80% covered.
- **Strict typing and lint:** pyright in `strict` mode, plus pylint.
- **The sandbox-escape suite** (`tests/integration/test_sandbox_escape.py`)
  brings up a real bottle with planted secrets and tries to get out: hosts
  and IPs off the allowlist, secrets in HTTP paths, bodies and headers, DNS
  exfiltration, and pushes through git-gate. Each attempt must be blocked.

## What runs on every launch

- **The launch-time isolation probe** (`backend/isolation_probe.py`) proves
  from inside the agent, before it runs, that it can't reach the host or a
  public address except through the gateway. If it can, the launch fails.
  Every backend runs it.

## What this doesn't promise

Review and tests reduce risk; they don't prove the absence of bugs, and that's
as true of human-written code as agent-written code. Report a vulnerability
as described in [`SECURITY.md`](SECURITY.md).
