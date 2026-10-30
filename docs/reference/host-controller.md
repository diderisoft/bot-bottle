# Host controller

`control/hostctl/` is the per-host daemon behind every bottle operation. The
CLI, the orchestrator and remote consoles all go through it
(PRD 0092, design repo). It runs as the user and adds
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
  hash of the package's `.py` files) differs from the CLI's. When that daemon
  holds bottles, a launch asks first ("moves N running bottles to the new
  version"); a headless or non-terminal launch is refused instead. Other
  commands keep using the old daemon. `bot-bottle up --restart` restarts it
  on purpose, with the same question. The daemon also refuses a launch itself
  once the code on disk no longer matches its `build_id`, so no path (the
  phone included) runs a launch on mixed code.
- `listeners.py` adds one listener per backend, where that backend's
  orchestrator guest reaches the host: the docker publish bridge, the macOS
  control network's vmnet gateway, or the firecracker orchestrator TAP. Each
  accepts only peers on its link and only guest roles (`orchestrator`,
  `gateway`). It never binds `0.0.0.0`.
- `bot-bottle install` can add a launchd LaunchAgent or a `systemd --user`
  unit (`service_unit.py`). A crash is restarted; a clean exit is not.
  Stopping the daemon stops only its own process (`KillMode=process`,
  `AbandonProcessGroup`), so agents outlive a restart. Units installed before
  this need `bot-bottle install` again.

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
  sessions and finishes ones that died while it was down. It then re-takes
  what running bottles hold in its process (`BottleBackend.hold_adopted`): on
  Firecracker, the TAP slot lock of each agent VM, whose VMM runs in its own
  session for this reason. A re-adopted session has no holder thread, so the
  holder leaves an `adoption.json` in the bottle's state dir at launch
  (`backend/adoption.py`: container name, registry id, workspace layout,
  deploy-key references; no secrets). When an adopted session ends, the daemon
  saves the agent's work, tears the bottle down through
  `BottleBackend.adopted_bottle`, and revokes its deploy keys with tokens from
  the secrets file. A token that was only in the launching CLI's environment
  can't be found then, so that key is reported for revoking by hand. Without a
  record, `bot-bottle cleanup bots` removes the bottle.
- `requests.py` polls every running orchestrator (one per backend with infra
  up, say docker and firecracker) for pending supervise proposals and shows a
  one-line summary in the session's tmux status bar, outside the agent's pane
  where the agent can't fake or hide it. Each proposal is tagged with the
  orchestrator holding it, and the terminal popup and the phone send its
  answer back there. One that's down or refuses the host's token is skipped.
- `blocks.py` hears the gateway's block events as the journal writes them
  (`HostAudit.listen`) and shows them in the same bar
  (PRD 0099, design repo). Each block shows for five
  seconds as `⊘ <host> · <reason>`; a burst shows the newest with `(+N)`,
  and a notice stays up at most ten seconds (then five seconds without one,
  so a retry loop can't keep it up). The session's counts by reason
  stay beside it as a letter and a number (`A12 S3 P1`):

  | Letter | Reason (`reason_code`) |
  |---|---|
  | A | not on allowlist (`allowlist`) |
  | R | outside the route's `matches`, or git over HTTPS (`route`) |
  | S | a secret in the request (`secret`) |
  | I | an unsafe response (`inbound`) |
  | M | a malformed request (`request`) |
  | D | you denied it (`denied`) |
  | P | git-gate rejected a push (`push`) |
  | C | bottle config: a token unset, an unattributed request (`config`) |
  | ? | an event without a code |

  The bar fits each client (`#{client_width}`): the notice's reason and the
  detach hint need 150 columns, and under 100 the request summary is cut.
- A launch's warnings (an isolation gap, a stale image) stay on the session
  as `@bb_warning` and show in amber before the detach key, shortened to the
  first warning's first clause and a count of the rest: up to 70 columns,
  30 under 100. A pending request takes their place.
  Counts live in memory and reset when the session or the daemon restarts.

Viewers:

| Viewer | Code | Transport |
|---|---|---|
| local terminal | `cli/commands/attach.py` | `tmux attach` |
| WebSocket | `attach.py` | binary frames for terminal bytes, JSON text frames for control |
| remote console | `console_terminal.py` | an iroh stream |

Every non-local viewer starts read-only. It can type only if the agent's
manifest sets `remote.writable`. The starter agents `bot-bottle setup` writes
set it, so a paired phone can talk to them; an agent written by hand is
view-only unless it says so.

## Transcripts

The daemon copies each session's harness transcript to the host as it's
written (`transcripts.py`; PRD 0095, design repo).
pi, Claude and Codex declare one (`AgentProvider.transcript_glob`); a
provider that doesn't isn't followed. `bot_bottle/transcript.py` holds the
entry type the parsers share (`contrib/<harness>/transcript.py`), with
`AgentProvider.active_entries` choosing the branch a tree-shaped transcript
(pi, Claude) has active.

- At launch a follower notes the files already matching the glob (another
  session's, never copied) and writes `follow.json`. Then, once a second and
  once more after the agent exits, one `exec` into the bottle reads each new
  file's new bytes by offset (up to 4 MiB a poll) and the 64 bytes before
  the offset. That `exec` gives up after 60 seconds, so a wedged one can't
  hold the follower, or the teardown waiting to close it. The daemon's other
  background `exec`s are bounded too (reading a bottle's secret, exporting a
  workspace's changes).
- The bytes are appended exactly as read to
  `~/.bot-bottle/transcripts/<session>/<activation>/<file>.jsonl` (0600, in
  0700 directories), with
  `<file>.meta.json` beside it: the guest path, the offset read and the
  console agent id (the console's for a console launch, otherwise
  `terminal_agent_id`). The copy isn't parsed or masked; entries are parsed
  when read (`AgentProvider.parse_transcript`).
- A guest file that shrinks, or whose bytes before the offset changed, keeps
  its copy as it was; the file is copied again from its start to
  `<file>.rewrite-<n>.jsonl`, with a `transcript.rewritten` audit event.
- `transcript.captured` records a copy's size and running SHA-256 every five
  minutes while it grows, when a restarted daemon resumes it, and when it
  closes (`final: true`). The payload
  marks it `agent-claimed`: the harness writes the file inside the bottle,
  so it's the agent's account, not evidence. The guest path is under
  `untrusted`.
- `transcript_retention.py` deletes a run's copies once the run is closed
  and unchanged for the host's retention (`bot-bottle audit
  transcript-retention [DAYS]`, default 30), at start and daily after, with
  a `transcript.deleted` event listing each copy's size and SHA-256.
- The console reads a conversation over the link (`transcript_serve.py`):
  `transcript` returns a page of the agent's entries from the copy its
  console agent id names, on the harness's active branch, and
  `transcript_appended` tells it when more landed. Entries are parsed when
  read and masked before they leave the host, with the egress scanner's
  `redact_tokens`: token patterns, and every value in the host's secrets
  file in its common encodings. The copy itself stays as read.
  `transcript_search` finds the newest entries containing a query
  (case-insensitive, over the masked text, so a query can't confirm a
  secret), optionally of given kinds, with a snippet around each match.
- A restarted daemon resumes each re-adopted session's copies from the
  saved offsets, exec'ing through `BottleBackend.attach_adopted` (which
  never tears the bottle down). Copies of sessions that ended while it was
  down close with what they have.

## Forwarded host logins

A bottle with `agent_provider.forward_host_credentials` gets the host's Claude
or Codex login read once at launch; the gateway injects it and the agent holds
a placeholder. The host's own CLI keeps refreshing its login, so every five
minutes the host controller re-reads it through the provider
(`AgentProvider.refresh_credentials`, `control/hostctl/credential_refresh.py`)
and pushes a value that changed to the orchestrator
(`POST /bottles/<id>/secret`), which the gateway reads per request. Codex's
placeholder `auth.json` in the guest carries the real token's expiry, so it's
rewritten when that moves.

This covers bottles the host controller launched and those it adopted after a
restart: the adoption record keeps which egress slots carry the login (nothing
secret), and the bottle's `ENV_VAR_SECRET` is read back from inside it. A host
login that's missing, expired or within six hours of expiring is logged once,
with what to run on the host, and audited as `credential.renew_failed`; a
refresh is audited as `credential.renewed`. bot-bottle doesn't spend the host's
refresh token itself (#769).

## Snapshots

The console's snapshot button asks the link for `snapshot`
(`snapshots.py`): the host attaches to the running bottle through its
adoption record (`BottleBackend.attach_adopted`), takes the copied git
workspace's patch since launch (the patch teardown saves), and keeps it as
`~/.bot-bottle/snapshots/<session>/<activation>/<taken>.patch` (0600) with a
`.json` record: when, the conversation's newest `seq` then, files and lines
changed, size and SHA-256. Each is audited as `snapshot.taken`. A workspace
that isn't a git copy can't be snapshotted. Restoring is not built yet.

## Audit

The host controller is the host's only audit writer
(PRD 0089, design repo). Producers are the host
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
With `bot-bottle pair` (or `pair on`), the daemon dials out to the console it logged
in to over iroh (ALPN `bb-console/1`). It accepts no inbound connections and
refuses a console whose node id isn't the one pinned at login. What pairing
keeps under `~/.bot-bottle/` (`remote.json`, `console.json`, the iroh key
`remote.key`, and `remote.status.json` for `bot-bottle status`) is
`pairing.py`.

The console can list local agents, launch a **local agent by name** (with an
optional prompt, bottle names and label), stop one, and view or type into
sessions. Agent and bottle definitions always come from this host, never from
the console. A launch, resume or stop runs beside the link, after that agent's
previous one, so the console's other requests are answered while it waits on
the bottle.

The console sees **every session on the host**, not only the ones it
launched. Each session record carries its `origin` (`console` or
`terminal`). The link presents each running `terminal` session with
`agent_present`, under an agent id derived from its bottle and activation
(`t-…`), so it keeps the same id across reconnects and host controller
restarts. It sends this on every connect and within a couple of seconds of a
session starting. After that the session is one of the link's agents like any
other: its requests are raised on the phone, its terminal is relayed (typing
still follows the agent's `remote.writable`), its history and audit journal
can be read, and the console can stop it. When the terminal and the phone
both answer a request, the first answer stands (see
[gateway.md](gateway.md#supervise)).

The console's Library asks for the host's `library`
(`control/hostctl/library.py`): each home agent and bottle under
`~/.bot-bottle/`, and the built-in bottles a home one doesn't shadow, with
their text (cut at 64 KiB). The console mirrors them read-only and keeps the
last copy for while the host is offline. Project bottles aren't sent.

The console's Manifest tab asks for a running agent's `manifest`
(`manifest_view.py`).

Each bottled agent has manifest versions (`run_manifest.py`,
`state/<bottle>/manifest/`). Version 1 is its whole bottle chain flattened
into one document (`manifest/document.py`), recorded at launch. A resume adds
a version only if the files changed. The `manifest` reply's `current` is the
latest version with the supervisor's changes since: egress routes the live
policy has beyond the launch routes (`state/<bottle>/egress/routes.yaml`), and
repos granted while it runs. It also lists what can't be kept (a route
carrying a credential) and a digest. `manifest_save` writes it as the next
version if the digest still matches. With `bottle` (one of the reply's
`bottle_targets`: the agent's chain, then the host's other bottles) it also
writes the run's changes into that bottle. With `new_bottle` it writes a new
host bottle of that name instead, recorded as a version too and refused if
the name is taken. Without `extend` it's flattened: the whole chain, no
`extends`. With `extend` it extends the agent's bottles and holds only the
run's changes. Adding a route for a host a bottle already has merges in the
new matches (`approvals.with_routes`), so nothing it allowed is dropped.
`keys` limits any of these to some of the run's changes (`egress:<host>`,
`repo:<name>`). With `remove`, they're taken away from the running bottle
instead. Its live policy is rendered again with those routes as they
launched (or without them) and set on the orchestrator. Repos granted while
it runs are revoked (`git_gate.live_grant.revoke_live`). A repo it launched
with is refused.

The reply's `elements` list everything the agent runs with
(`manifest_elements.py`). Each egress route, repo, forge and env var is keyed
`<kind>:<name>`, with its source: the nearest bottle file in the chain that
declares it (the others are listed too), "this run", or "agent provider".
A repo granted this run shows its grant, and the forge it's on is listed with
the host bottle that declares it when nothing in the chain does.
`manifest_transfer` copies picked elements into a bottle, or moves them out of
the file they came from. A built-in bottle's elements, and this run's, are
copied. With `delete` it takes picked elements out of the file they come from
(the running agent keeps them until its next launch), and this run's out of
the running bottle. A built-in bottle's and the provider's stay. Each file
changed is saved as a new version.
