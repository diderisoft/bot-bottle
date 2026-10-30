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
TAP, with the orchestrator and gateway in their own per-host microVMs. The legacy Docker
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

- `README.md` — short public-facing getting-started page.
- `AGENTS.md` — this file, orientation for future agent sessions.
- `CONTRIBUTING.md` — commit format and the PR tracker policy;
  `AI_POLICY.md` and `SECURITY.md` sit beside it.
- `.gitignore` — OS junk.
- `.bot-bottle/` — per-repo agent and bottle manifests (YAML markdown format).
- `examples/` — example bottles and agents showing the manifest format.
- `docs/README.md` — docs overview; when to write which document.
- `docs/reference/` — how the system works **now**. Read
  `docs/reference/architecture.md` first; it links the rest (backends,
  install, manifest, gateway, git-gate, host controller, runtime contract,
  glossary).
- `docs/decisions/` — decision records (ADR-lite): why the system is
  the way it is.

## Conventions

- Reference pages (`docs/reference/`) describe current behaviour and are
  edited in place. A change that alters behaviour a reference page
  describes updates that page in the same PR.
- Design rationale lives in `docs/decisions/` and `docs/reference/`.
  Decision records are ADR-lite, numbered `NNNN-kebab.md`, for policies,
  boundaries and load-bearing choices; see `docs/decisions/README.md`
  and `docs/README.md`.
- PRDs and research notes live in the private `diderisoft/bot-bottle-design`
  repo (ADR 0010), for those who have access. This repo must make sense
  without it: mention a PRD in plain text ("PRD 0081 (design repo)"),
  never link into it, and when a choice in one matters to readers here,
  write it into a reference page or decision record.
- Keep decision rationale in a document, not in Gitea issue threads.
  Issues are an ephemeral inbox.
- Low dependencies by default. The project is Python, stdlib-first (no
  runtime pip dependencies in the package itself; the only language
  runtime is the Python 3.13 used by the CLI and the orchestrator and gateway images). Ask before
  adding new tools, runtimes, or package managers.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):
  `<type>[(scope)][!]: <description>`, where `<type>` is one of `feat`, `fix`,
  `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`.
  A `commit-msg` hook in `.githooks/` enforces this. Activate it once per clone
  with `git config core.hooksPath .githooks`.

## When you're unsure

Ask. Default to drafting in chat over editing files when the request is ambiguous.
