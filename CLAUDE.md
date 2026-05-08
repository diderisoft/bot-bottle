# claude-bottle

## What this is

claude-bottle spins up an isolated container for running Claude Code with a
curated set of skills and env vars. The point is to run Claude with broad
permissions inside a sandbox, so a misbehaving agent cannot reach the host.
Bash scripts orchestrate the container lifecycle and the copying of skills
and env vars into it.

## Goals

- Minimize risk of running claude with full permissions
- Allow me to easily spin up agent tasks in parallel
- Create isolated, well defined, easily updated, shareable agents

## Non-goals

- Communicating between agents directly
- Self hosted VMs (v1 uses local Docker containers, not VMs)
- Advanced agent auditing (lean on git history for auditing)

## Repository layout

- `README.md` — short public-facing description.
- `CLAUDE.md` — this file, orientation for future Claude sessions.
- `.gitignore` — OS junk.
- `claude-bottle.json` — manifest of named agents (env / skills / prompt
  per agent), consumed by `cli.sh`. See "Manifest" under
  "Intended design".
- `docs/INDEX.md` — pointer to the journal and research notes.
- `docs/JOURNAL.md` — append-only log of decisions and state changes.
- `docs/prds/` — product requirement docs.
- `docs/research/` — research notes (empty for now, kept tracked via `.gitkeep`).
- `.claude/skills/init-entry/` — project-local Claude Code skill providing `/init-entry` for adding journal entries. Snapshotted from `~/.claude/skills/init-entry/` at scaffold time; refresh deliberately if it drifts.

The container launcher scripts (`Dockerfile`, `cli.sh`,
`lib/*.sh`) landed in PRD 0001 and were
extended in PRD 0002 with `lib/manifest.sh`,
`lib/env_resolve.sh`, and `lib/skills.sh`. Note: any
future repo-root `skills/<name>/` directory (skills sent into the
container) is a distinct concept from `.claude/skills/<name>/` (Claude
Code skills used while working in this repo) — don't conflate them.

## Conventions

- Text-driven content. `docs/JOURNAL.md` is an append-only stream of thought,
  newest first. Entries are timestamps followed by freeform prose — no
  templates, no required sections. Add entries with `/init-entry`.
- Product requirement docs live in `docs/prds/`.
- Research notes live in `docs/research/`.
- Low dependencies by default. The project is bash-first; ask before adding new
  tools, runtimes, or package managers.

## Intended design

PRD 0002 lands the manifest-driven agent flow described below. The
`defaults/` directory and the repo-side `skills/` snapshot/diff loop
sketched at scaffold time are deferred — see "Deferred from the
scaffold sketch" at the end of this section.

### Manifest

Per-agent configuration lives in `claude-bottle.json` under an `"agents"` key.
`cli.sh` looks for this file in two locations and merges them:

1. **Current working directory** (`$PWD/claude-bottle.json`) — project-local agents.
2. **Home directory** (`$HOME/claude-bottle.json`) — personal global agents.

If both exist, the two `agents` objects are merged (home is the base, cwd
entries win on a same-agent-name conflict).
If neither file exists, `cli.sh` dies with a clear message.

Each agent has three attributes:

- `env` — hash of env vars. Each value is a JSON string whose mode
  is selected by sentinel prefix:
  - `"?<message>"` — value is prompted at runtime from `/dev/tty`
    (silent), exported into the launcher process, and forwarded to
    the container via `docker run -e NAME` (no `=value`). Never
    written to disk, never on argv. The launcher always asks, even
    if a same-named var is already in the parent shell. `<message>`
    is rendered verbatim as the prompt body; the launcher appends
    ` (input hidden): `. Bare `"?"` is allowed and falls back to a
    default `claude-bottle: secret value for NAME` prompt.
  - `"${HOST_VAR}"` — exact `${IDENT}` form, where `IDENT` matches
    `[A-Za-z_][A-Za-z0-9_]*`. Value is read from `$HOST_VAR` in the
    host process env at launch time. Treated the same as a secret on
    the wire: copied into this process under the target name,
    forwarded as `-e NAME` (no `=value`), never written to disk.
  - any other string — literal value, hardcoded in the manifest.
    Written to a mode-600 env-file under `mktemp -d` and passed to
    docker via `--env-file`. Newlines are rejected up front because
    docker `--env-file` cannot represent them. A literal whose text
    starts with `?` or matches `${IDENT}` is not representable in
    v1 — pick a different value or revisit the convention.
- `skills` — list of skill names. Each is `docker cp`'d from
  `~/.claude/skills/<name>/` into the running container's
  `~/.claude/skills/<name>/`, preserving per-skill directory structure
  (no flattening, no archives). If a referenced skill is missing on
  the host, `cli.sh` fails with a clear message naming the skill
  and the path checked. The host→repo fallback and host↔repo diff
  prompt described in the original sketch are deferred.
- `prompt` — string prepended to the chat when the container session
  boots. Delivered by writing the string to a file inside the
  container via `docker cp` (so the prompt content does not land on
  `docker exec` argv) and passing it to
  `claude --append-system-prompt-file <path>`. Note: as of the
  claude-code version pinned in the Dockerfile, this flag is real but
  is not surfaced in the alphabetized `claude --help` output (only
  mentioned obliquely under `--bare`); a future rename or removal will
  break the launcher with a clear error from claude itself. Bare
  `start` (no `<name>`) is intentionally not supported — `<name>`
  remains required.
- `ssh` — optional array of SSH host entries. Each entry is an object
  with five required keys:
  - `Host` — the `Host` alias written to `~/.ssh/config` in the
    container (also the name you use as the ssh destination).
  - `IdentityFile` — absolute path to the private key file on the host
    (leading `~` is expanded). At launch the key is `docker cp`'d into
    `/root/.claude-bottle-keys/` (mode 700, root-owned), loaded into a
    root-owned `ssh-agent` listening on `/run/claude-bottle-agent.sock`,
    and the key file is then deleted. The agent socket is `chmod 666`
    so the `node` user can connect; the agent protocol only exposes
    signing operations, never the key bytes. Keys must be
    passphrase-less (no TTY for `ssh-add` to prompt against).
  - `Hostname` — the actual hostname or IP for `HostName`.
  - `User` — the SSH username for `User`.
  - `Port` — the SSH port number for `Port`.
  - `KnownHostKey` — (optional) the host's public key, written to
    `~/.ssh/known_hosts` under both the `Host` alias and the
    `Hostname` (so the lookup succeeds whether the connection uses
    the alias or the raw IP/host, e.g. a git remote URL with the
    bare IP). Eliminates the interactive host-verification prompt on
    first connect.

  Per-Host blocks in `~/.ssh/config` use `IdentityAgent
  /run/claude-bottle-agent-public.sock` rather than `IdentityFile`, so SSH
  always reaches the agent regardless of `SSH_AUTH_SOCK`. The public
  socket is served by a root-owned `socat` forwarder, not by the agent
  itself: OpenSSH's `ssh-agent` enforces a `SO_PEERCRED`-based UID-match
  check on every connection (only accepts peers with euid 0 or matching
  the agent's own uid), so non-root callers like `node` are rejected
  even when the socket is mode 666. `socat` runs as root, accepts node's
  connections on the public socket, and proxies to the real agent socket
  at `/run/claude-bottle-agent.sock`; from the agent's perspective the peer
  is uid 0 and passes the check.

  Why an in-container agent (not a bind-mounted host agent): Docker
  Desktop on macOS does not forward Unix-domain socket `connect()`
  across the macOS↔Linux VM boundary (returns `ENOTSUP`). Running the
  agent inside the container sidesteps that while preserving the
  isolation property we want (node can use the key for SSH but cannot
  read the bytes — root-owned agent and forwarder, no `CAP_SYS_PTRACE`).

  `cli.sh start` validates that every key file exists on the host
  before the y/N prompt, then after the container is running it spawns
  the in-container `ssh-agent`, loads the keys, deletes the key files,
  and writes `~/.ssh/config` (mode 600) with one `Host` block per
  entry.

Agent keys (the top-level keys of `claude-bottle.json`) should already be
slug-friendly (lowercase, alphanumeric + hyphens). The container name
is `claude-bottle-<slug>`, with a numeric suffix appended on conflict —
so two parallel starts of the same agent get distinct containers
(`claude-bottle-journal`, `claude-bottle-journal-2`, ...) instead of the
second failing. Two distinct agent keys that slug to the same value
(e.g. `"Review PR"` and `"review-pr"`) will both work but become hard
to tell apart in `docker ps`; pick keys that are already slugs to
avoid that ambiguity. `CLAUDE_BOTTLE_CONTAINER` still pins an exact name
and keeps the strict-conflict failure if it's already taken.

### Confirmation

Before launching a container, `cli.sh` shows the resolved plan and
waits for a single `y/N`:

- agent name, image, container name
- env var names (never values; secrets are also never identified
  separately, since the name itself plus the manifest is the source
  of truth)
- skill names being sent
- prompt length and first line only

Pass `--dry-run` (or set `CLAUDE_BOTTLE_DRY_RUN=1`) to print the plan
and exit before any `docker run` / `docker cp` / `docker exec`.

### Deferred from the scaffold sketch

The pre-PRD-0002 sketch described a repo-root `skills/<name>/`
snapshot, a `defaults/secrets.json` + `defaults/config.env`, and a
host↔repo skill diff loop. None of that is implemented; v1 reads the
manifest, prompts secrets, forwards literals via `--env-file`, and
copies host skills directly into the container. Reopen the question
in the journal if and when the snapshot story matters.

## When you're unsure

Ask. Default to drafting in chat over editing files when the request is ambiguous.
