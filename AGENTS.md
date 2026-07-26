# bot-bottle

## What this is

bot-bottle spins up an isolated backend runtime for running AI coding agents
with a curated set of skills and env vars. The point is to run agents with
broad permissions inside a sandbox, so a misbehaving agent cannot reach the
host. A Python CLI (entry point `cli.py`, package `bot_bottle/`) orchestrates
the runtime lifecycle and the copying of skills and env vars into it.
The default backend on compatible macOS hosts is macos-container:
agents and gateways run through Apple's `container` CLI without
requiring Docker. On KVM-capable Linux hosts the default is firecracker:
agents run in a Firecracker microVM reached over SSH on a point-to-point
TAP, while the gateway still uses Docker. The legacy Docker
backend remains available with `BOT_BOTTLE_BACKEND=docker` or
`--backend=docker`.

## Goals

- Minimize risk of running agents with full permissions
- Allow me to easily spin up agent tasks in parallel
- Create isolated, well defined, easily updated, shareable agents

## Non-goals

- Communicating between agents directly
- Removing the Docker backend
- Advanced agent auditing (lean on git history for auditing)

## Repository layout

- `README.md` — short public-facing description.
- `AGENTS.md` — this file, orientation for future agent sessions.
- `.gitignore` — OS junk.
- `.bot-bottle/` — per-repo agent and bottle manifests (YAML markdown format).
- `examples/` — example bottles and agents showing the manifest format.
- `docs/README.md` — docs overview; when to write which document.
- `docs/glossary.md` — canonical term definitions (Agent Provider, Bottle, Sealed Bottle, etc.).
- `docs/prds/` — product requirement docs (see `docs/prds/README.md` for format).
- `docs/research/` — research notes (see `docs/research/README.md`).
- `docs/decisions/` — decision records (ADR-lite).

## Conventions

- Three kinds of doc, each with its own conventions in-folder; see
  `docs/README.md` for when to write which:
  - **PRDs** (`docs/prds/`) — one feature per file. A draft may initially
    use `prd-new-<kebab>.md`, but its author must assign the next
    sequential number before merge; CI rejects unnumbered PRDs. A
    `Status:` line tracks lifecycle: Draft → Active (shipped to `main`) →
    Superseded/Retargeted. Format in `docs/prds/README.md`.
  - **Research notes** (`docs/research/`) — opinionated investigations;
    unnumbered kebab-case, freeform and verdict-first. See
    `docs/research/README.md`.
  - **Decision records** (`docs/decisions/`) — ADR-lite, numbered
    `NNNN-kebab.md`, for policies and non-feature decisions. See
    `docs/decisions/README.md`.
- Keep decision rationale self-contained in the repo, not in Gitea
  issue threads. Issues are an ephemeral inbox; the durable "why" lives
  in a PRD, research note, or decision record.
- Low dependencies by default. The project is Python, stdlib-first (no
  runtime pip dependencies in the package itself; the only language
  runtime is the Python 3.13 used by the CLI + companion containers). Ask before
  adding new tools, runtimes, or package managers.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):
  `<type>[(scope)][!]: <description>`, where `<type>` is one of `feat`, `fix`,
  `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`.
  A `commit-msg` hook in `.githooks/` enforces this. Activate it once per clone
  with `git config core.hooksPath .githooks`.

## When you're unsure

Ask. Default to drafting in chat over editing files when the request is ambiguous.
