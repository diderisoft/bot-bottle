# bot-bottle

## What this is

bot-bottle spins up an isolated container for running AI coding agents with a
curated set of skills and env vars. The point is to run agents with broad
permissions inside a sandbox, so a misbehaving agent cannot reach the host.
A Python CLI (entry point `cli.py`, package `bot_bottle/`) orchestrates
the container lifecycle and the copying of skills and env vars into it.

## Goals

- Minimize risk of running agents with full permissions
- Allow me to easily spin up agent tasks in parallel
- Create isolated, well defined, easily updated, shareable agents

## Non-goals

- Communicating between agents directly
- Self hosted VMs (v1 uses local Docker containers, not VMs)
- Advanced agent auditing (lean on git history for auditing)

## Repository layout

- `README.md` — short public-facing description.
- `AGENTS.md` — this file, orientation for future agent sessions.
- `.gitignore` — OS junk.
- `bot-bottle.json` — legacy manifest of named agents (env / skills / prompt
  per agent), consumed by `cli.py`. See "Manifest" under
  "Intended design".
- `docs/INDEX.md` — pointer to the research notes.
- `docs/prds/` — product requirement docs.
- `docs/research/` — research notes (empty for now, kept tracked via `.gitkeep`).

## Conventions

- Product requirement docs live in `docs/prds/`.
- Research notes live in `docs/research/`.
- Low dependencies by default. The project is Python, stdlib-first (no
  runtime pip dependencies in the package itself; the only language
  runtime is the Python 3.13 used by the CLI + sidecars). Ask before
  adding new tools, runtimes, or package managers.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):
  `<type>[(scope)][!]: <description>`, where `<type>` is one of `feat`, `fix`,
  `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`.
  A `commit-msg` hook in `.githooks/` enforces this. Activate it once per clone
  with `git config core.hooksPath .githooks`.

## When you're unsure

Ask. Default to drafting in chat over editing files when the request is ambiguous.
