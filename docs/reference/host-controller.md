# Host controller

`control/hostctl/` is the per-host daemon behind every bottle operation. The
CLI, the orchestrator and remote consoles all go through it
([PRD 0092](../prds/0092-host-controller.md)). It runs as the user and adds
no privilege: it gives every caller one API, and it is not a security
boundary. Its routes are in
[runtime-contract.md](runtime-contract.md#host-controller-as-the-orchestrator-sees-it).

## Running

- `python -m bot_bottle.control.hostctl` binds `127.0.0.1`, on
  `BOT_BOTTLE_HOSTCTL_PORT` or an ephemeral port. It writes
  `{pid, port, build_id}` to `~/.bot-bottle/hostctl/`.
- The CLI trusts that file only once `GET /status` returns the same pid.
  `runner.ensure_running` starts the daemon on demand, holding a lock so
  concurrent CLIs start only one, and restarts a daemon whose `build_id` (a
  hash of the package's `.py` files) differs from the CLI's.
- `listeners.py` adds one listener per backend, where that backend's
  orchestrator guest reaches the host: the docker publish bridge, the macOS
  control network's vmnet gateway, or the firecracker orchestrator TAP. Each
  accepts only peers on its link and only guest roles (`orchestrator`,
  `gateway`). It never binds `0.0.0.0`.
- `bot-bottle install` can add a launchd LaunchAgent or a `systemd --user`
  unit (`service_unit.py`). A crash is restarted; a clean exit is not.

## Jobs

Launch, teardown and orchestrator bring-up run as jobs (`jobs.py`). Each job
runs in its own thread with its output captured as events: `log` lines, then
one `result` or `error`. Clients stream them from `GET /jobs/{id}/events`.
Jobs are held in memory and dropped `RETENTION_SECONDS` after they finish.

`bottles.py` runs a launch job. It prepares the plan without prompting (the
CLI has already asked everything), then hands it to a holder thread that
stays inside `backend.launch(plan)` for the bottle's life and tears it down
when the agent exits.

## Sessions

Each agent runs in tmux on bot-bottle's own tmux server, never the user's
(`sessions.py`):

- The pane runs `launch.py`, which fetches the real command and env over a
  0600 Unix socket (`localsock.py`) once. Secrets never reach tmux's argv or
  environment.
- tmux's `pane-died` hook runs `notify.py` to report the exit. The session's
  finisher then runs, and tmux detaches every viewer.
- Session records are written to disk. A restarted daemon re-adopts live
  sessions and finishes ones that died while it was down. A re-adopted
  session has no holder thread, so `bot-bottle cleanup` removes its bottle.
- `requests.py` polls the orchestrator for pending supervise proposals and
  shows a one-line summary in the session's tmux status bar, outside the
  agent's pane where the agent can't fake or hide it.

Viewers:

| Viewer | Code | Transport |
|---|---|---|
| local terminal | `cli/commands/attach.py` | `tmux attach` |
| WebSocket | `attach.py` | binary frames for terminal bytes, JSON text frames for control |
| remote console | `console_terminal.py` | an iroh stream |

Every non-local viewer starts read-only. It can type only if the agent's
manifest sets `remote.writable`.

## Audit

The host controller is the host's only audit writer
([PRD 0089](../prds/0089-audit-event-schema.md)). Producers are the host
controller itself (`lifecycle.*`, `hostctl.*`), the orchestrator, and the
gateway's `egress`, `git-gate` and `supervise`. Gateway events reach it
through the orchestrator (`POST /audit/append`).

| Module | Does |
|---|---|
| `audit_journal.py` | append-only JSONL segments in `~/.bot-bottle/audit/`. Each record hash-chains to the one before it across segments and restarts. An exclusive directory lock enforces one writer. Credential-shaped keys are refused and token-shaped values scrubbed before writing. `id` is the idempotency key. |
| `audit_index.py` | a SQLite index rebuilt from the journal, for `bot-bottle audit query`. It stops at the first record that doesn't chain. |
| `audit_verify.py` | offline chain check (`bot-bottle audit verify`). Reports `truncated-tail`, `unreadable`, `edited`, `gap` and `chain-break`. |
| `audit_forward.py` | at-least-once forwarding to an HTTPS endpoint, as native NDJSON or OCSF. The cursor advances only on a 2xx. |
| `audit_ocsf.py` | maps a journal record to OCSF 1.9.0. `restricted` records ship only the envelope and chain fields. |
| `audit_import.py` | stores segments from another host verbatim under `imported/<host>/<chain>/`, never joined to the local chain |

A journal that can't be written is logged, and the launch or teardown still
goes ahead. Where a bottle sets `audit.delivery: required`, egress and
git-gate refuse the action they couldn't record (see
[gateway.md](gateway.md)).

## Firehose

`firehose.py` and `firehose_sinks.py` fan the gateway's per-request records
out to `bot-bottle audit tail` subscribers and to configured sinks:

- `exec`: a command that gets JSON lines on stdin, restarted with backoff.
- `https`: NDJSON batches with a bearer token from the secrets file.

Each consumer has its own bounded queue. A consumer that falls behind drops
its oldest records and is told how many. Nothing is stored. When nothing is
consuming, the gateway stops building records. Settings are re-read every
`RELOAD_SECONDS`.

## Remote access

`remote.py` ([ADR 0007](../decisions/0007-remote-access-terminates-in-host-controller.md)).
With `bot-bottle remote enable`, the daemon dials out to the console it logged
in to over iroh (ALPN `bb-console/1`). It accepts no inbound connections and
refuses a console whose node id isn't the one pinned at login.

The console can list local agents, launch a **local agent by name** (with an
optional prompt, bottle names and label), stop one, and view or type into
sessions. Agent and bottle definitions always come from this host, never from
the console.
