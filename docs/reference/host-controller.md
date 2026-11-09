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
  on purpose, with the same question; `--yes` answers it, for scripts and
  services with no terminal (without either, they're refused). It leaves a
  current orchestrator and gateway running (`--restart-infra` reboots them;
  see [backends](backends.md#gateway-restarts)). The daemon also refuses a launch itself
  once the code on disk no longer matches its `build_id`, so no path (the
  phone included) runs a launch on mixed code.
- An upgrade under a running daemon is reported before a launch fails:
  `bot-bottle status` and `bot-bottle doctor` (which the installer runs) print
  "restart needed (installed version changed)" with the `up --restart` fix,
  and the console link's `host_stats.about` carries `restart_needed`. Nothing
  restarts the daemon on its own.
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
when the agent exits. A launch the host's budget can't fit is refused
before it boots ([Bottles' budget](#bottles-budget)).

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
- `requests.py` polls the environment's orchestrator (the one its last
  bring-up recorded; [architecture.md](architecture.md#components)) for
  pending supervise proposals and shows a one-line summary in the session's
  tmux status bar, outside the agent's pane where the agent can't fake or hide
  it. Each proposal is tagged with that orchestrator's URL, and the terminal
  popup and the phone send its answer back there.
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

The console's relay is one tmux client per agent, sized by the console's
`resize` (the most recent viewer's terminal). The link keeps the last size
asked for each running agent and opens every new relay at it: a relay
reopened after a broken stream would otherwise start at tmux's default, and
a terminal of a different size draws its output garbled.

## Transcripts

The daemon copies each session's harness transcript to the host as it's
written (`transcripts.py`; PRD 0095, design repo).
pi, Claude and Codex declare one (`AgentProvider.transcript_glob`); a
provider that doesn't isn't followed. `bot_bottle/transcript.py` holds the
entry type the parsers share (`contrib/<harness>/transcript.py`), with
`AgentProvider.active_entries` choosing the branch a tree-shaped transcript
(pi, Claude) has active.

- At launch a follower notes the files already matching the glob (another
  session's, never copied) and writes `follow.json`. A resume (the bottle's
  home kept, its harness continuing its last conversation, as Claude's
  `--continue` appends to the file it was writing) notes each file's size
  instead, in `follow.json` as `start`: what the agent adds to a file is
  copied from there, the earlier run's copy holding the rest. Then, once a second and
  once more after the agent exits, one `exec` into the bottle reads each new
  file's new bytes by offset (up to 4 MiB a poll) and the 64 bytes before
  the offset. A quiet transcript is polled less often, doubling up to every 5
  seconds; new bytes, or the console typing into the agent, bring it back to
  every second. The poll's script (paths and offsets) goes as an argument,
  not on stdin: on macOS every `container exec --interactive` leaks a pipe in
  the bottle's runtime ([apple/container#2439](https://github.com/apple/container/issues/2439)),
  which polled each second ran the host out of open files. That `exec` gives up after 60 seconds, so a wedged one can't
  hold the follower, or the teardown waiting to close it. The daemon's other
  background `exec`s are bounded too (reading a bottle's secret, exporting a
  workspace's changes).
- The bytes are appended exactly as read to
  `~/.bot-bottle/transcripts/<session>/<activation>/<file>.jsonl` (0600, in
  0700 directories), with
  `<file>.meta.json` beside it: the guest path, the offset read, the guest
  offset the copy starts at (`start`, for a resumed run's copy) and the
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
  `transcript` returns a page of the agent's entries, on the harness's
  active branch. Each session file (a `/clear` starts a new one) is put
  together from its copies across the bottle's runs in the order they were
  made: a resumed run's copy continues at its `start`, a rewrite starts the
  file over. An entry's `seq` is its place in its file plus a stride per
  earlier session, so it never moves or names another entry. A page is from
  the current session (since the last `/clear`), with `first` and `last`
  bounding it; a cursor (`before`, `around`) into an earlier session, such
  as a search hit from before a `/clear`, `all: true`, or a current session
  with nothing in it yet reads the whole history instead. `scope` says which
  (`current` or `all`); seqs are the same in both. And
  `transcript_appended` tells it when more landed. Entries are parsed when
  read and masked before they leave the host, with the egress scanner's
  `redact_tokens`: token patterns, and every value in the host's secrets
  file in its common encodings. The copy itself stays as read.
  `transcript_search` finds the newest entries containing a query
  (case-insensitive, over the masked text, so a query can't confirm a
  secret), optionally of given kinds, with a snippet around each match.
- `agent_working` tells the console whether a running agent is working on
  a prompt, sent when that changes (and once per agent on each connect).
  The harness decides from the newest lines of its copy
  (`AgentProvider.working`): Claude's turn ends at `turn_duration` or a reply
  that ends the turn, and starts with a prompt or a tool call; Codex's runs
  from `task_started` to `task_complete` or `turn_aborted`. A harness without
  markers (the default) counts as working while its copy grew within the last
  20 seconds. It's the agent's own output, so it's an indicator for the
  console only, never a security decision.
- `host_stats` reports this host's storage and memory on each connect and
  every 60 seconds (`host_stats.py`), host-wide and against the bottles'
  budget (see [Bottles' budget](#bottles-budget)), all in bytes:

  ```
  {"storage": {"used", "total", "cap"},
   "memory": {"used", "total", "cap"},
   "budget": {
     "memory": {"allocated", "budget", "bottle_size",
                "bottles": [{"id", "label", "memory", "cpu_pct", "mem_used"}]},
     "storage": {"used", "budget",
                 "categories": {"vm_disks", "images", "workspaces", "transcripts"},
                 "unmeasured": ["<runtime>: <why>"]}},
   "about": {"os", "backend", "version", "commit", "restart_needed"},
   "forge_alerts": [{"key", "kind", "forge", "message", "since"}]}
  ```

  The host-wide `storage` covers the filesystems holding the state root and
  the caches, each counted once; `memory` is what the kernel reports in use
  (`MemTotal - MemAvailable` on Linux; free and inactive pages on macOS).
  `cap` repeats the budget for consoles that predate the `budget` block. A
  budget of 0 means none is set. `budget.storage` is left out until the
  first storage walk after the daemon starts. `about.restart_needed` is true
  once bot-bottle was upgraded under the running daemon: it refuses launches
  until `bot-bottle up --restart`.

  `forge_alerts` are forge tokens that stopped working for good, one per
  credential (`key` is the host secret's name), oldest first
  (`bot_bottle/forge_alerts.py`). `kind` is `github_reauthorize` (the GitHub
  authorization can't be renewed: its refresh token expired or was revoked;
  raised by the git-gate token renewer) or `gitea_token_refused` (the forge
  answered 401/403 when a launch, grant or teardown used the token). Each is
  raised once per failure state and cleared when the credential works again
  or is replaced (`forge connect`); network trouble is only a warning. They
  live in the host controller's memory: after a restart the next renewal or
  launch raises them again.

  `about` is what the host's console page says about it: its OS (`macOS`,
  or a Linux's `NAME` from `/etc/os-release`), the backend its last
  bring-up recorded (`""` before one) and this install's version and short
  commit (as `bot-bottle version` prints them).

  Each running bottle's `cpu_pct` and `mem_used` (bytes) are left out when
  they can't be read (`bottle_metrics.py`). CPU % is a share of the
  bottle's CPUs, 0-100: a Firecracker VM's vCPUs from its `config.json`
  (one busy vCPU of two is 50%), and a container's `stats` figure capped at
  100, since bottles set no container CPU limit to scale it by.
- `metrics_watch` (`bottles`: bottle ids) replaces the link's watched set,
  for the console's live gauge on the bottles someone is looking at; an
  empty list stops it. While it's non-empty the link samples the watched
  bottles it has running in one pass every 3 seconds, on every backend, and
  sends `bottle_metrics` (`at`, `bottles`: `id`, `cpu_pct`, `mem_used`,
  `memory`) (`live_metrics.py`). The interval leaves room for a container's
  `stats` call (1-2 s each); a pass still running when the next is due skips
  it. The set is the connection's, capped at 32: a reconnecting console
  sends it again. The live samples keep their own CPU baseline, so the
  minute `host_stats` figure still covers its whole minute.
- A restarted daemon resumes each re-adopted session's copies from the
  saved offsets, exec'ing through `BottleBackend.attach_adopted` (which
  never tears the bottle down). Copies of sessions that ended while it was
  down close with what they have.

## Bottles' budget

`bot-bottle host budget --memory GB --disk GB` sets what bottles may use on
this host (0 clears one; no flags shows them). It's stored in the host DB and
read at each launch and each `host_stats` report, so a change applies at once.
`bot-bottle host caps` is its deprecated old name.

- **Memory is allocation.** A bottle is given 2 GiB (`backend/bottle_size.py`)
  unless its `resources:` say otherwise ([backends.md](backends.md#memory-and-vcpus)):
  a firecracker VM's memory, a macOS container's `--memory`, a Docker agent's
  `mem_limit`. The session record keeps what each was given (`memory`), and
  allocated is the sum over the bottles running: held, adopted after a restart,
  launching, or with a running session. A bottle recorded before `resources:`
  counts at 2 GiB. A VM usually uses less than it's given, so
  allocation can sit well above the host-wide figure.
- **Storage is bot-bottle's own directories, plus what the container
  runtimes hold for it.** The directories are the state root and the
  artifact and firecracker caches, in allocated blocks (a sparse VM disk
  counts what it holds). Every runtime on the PATH is asked too
  (`backend/runtime_store.py`): Docker through `docker system df -v`, Apple
  `container` through `container image list` and its store under
  `~/Library/Application Support/com.apple.container` (`containers/`,
  `volumes/`). A background thread (`budget.StorageMeter`) measures every
  five minutes; launches read the last result and never measure.
  Categories: `vm_disks` (firecracker run dirs, the infra VMs' disks, kept
  `home.ext4` disks, and this environment's Docker or `container` containers'
  writable layers and volumes, by its resource prefix), `transcripts`
  (transcripts, audit, logs), `images` (the rest of the caches, the build
  root, and the runtimes' `bot-bottle-*` images), `workspaces` (the rest of
  the state root). A runtime image's unique bytes count, and the layers it
  shares count once (the largest share), so runtime images are an estimate.
  Docker's build cache isn't attributable and isn't counted. The caches and
  runtime images are shared by every environment on the host. A runtime that
  can't be asked is listed in `budget.storage.unmeasured` (`"docker: <why>"`)
  rather than counted as zero.

`BottleService._launch` admits a launch against the budget after preparing
it and before booting, one admission at a time (so two launches can't both
take the last room); an admitted bottle counts as launching until its holder
takes it. It refuses, failing the launch job (the console sees
`agent_errored` with the message):

- when the bottle's memory doesn't fit: "<label> needs 2 GB of memory; 0 GB
  left in the 8 GB budget. Stop a bottle (`bot-bottle bots`) or raise the
  budget (`bot-bottle host budget --memory …`)."
- when bot-bottle's storage is over its budget, naming
  `bot-bottle cleanup images` to free cached images.

At 90% of the storage budget or more, a launch goes ahead with a warning. No
budget set, no check.

## Forwarded host logins

A bottle with `agent_provider.forward_host_credentials` gets the host's Claude
or Codex login read once at launch; the gateway injects it and the agent holds
a placeholder. The host's own CLI keeps refreshing its login, so every five
minutes the host controller re-reads it through the provider
(`AgentProvider.refresh_credentials`, `control/hostctl/credential_refresh.py`)
and pushes a value that changed to the orchestrator that registered the
bottle, the environment's recorded one (`POST /bottles/<id>/secret`), which the
gateway reads per request. A push that orchestrator can't take (down, a
rejected key, an unknown bottle) fails naming it. Only an environment with no
orchestrator recorded yet (brought up before bring-ups recorded one) tries
each listening orchestrator in turn, treating a 401/403, a 404 or no answer as
"not this one", and fails only when none accepted, listing each URL and why. Codex's
placeholder `auth.json` in the guest carries the real token's expiry, so it's
rewritten when that moves.

The host's CLI revokes the old access token the moment it rotates its login,
so the timer alone left bottles on a revoked token (`401 OAuth access token has
been revoked`) for up to five minutes. The host controller also watches the
files each provider names (`AgentProvider.host_login_paths`: Claude's
`~/.claude/.credentials.json`, Codex's `auth.json`) every five seconds. When
one changes and reads whole (a half-written file waits for the next look), the
bottles forwarding that harness's login are refreshed at once, through the
same path. The timer stays as the backstop, and is all a login kept outside a
file gets (the macOS Keychain).

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

## Bottle suggestions

Every bottle suggestion runs here, in the host controller, which keeps the
bottle architect's own bottle warm (`architect_bottle.py`;
[bottles-suggest.md](bottles-suggest.md#bottle-architect-default)). The CLI
asks over HTTP, `cli` role only: `POST /suggest` (`task`, optional `cwd`,
`rules`, `settings`, and `turns`, the conversation so far after a reply) starts a `bottle.suggest` job whose `result` is
`{"proposal": ...}`, and `POST /architect/warm` (`settings`) boots the warm
bottle in the background and answers its `state` (`booting`, `ready`,
`down`, or `unavailable`). The host controller boots the bottle when it
starts and keeps it up (`BOT_BOTTLE_ARCHITECT_ALWAYS_ON`, the default; 512
MiB, one CPU). Without it, `bot-bottle start` sends the warm request as soon
as it starts and the bottle stays up `BOT_BOTTLE_ARCHITECT_IDLE` seconds
(default 600) after its last suggestion. Either way the host controller
tears it down when it stops, and replaces one removed from under it on the
next suggestion. Its start and stop are audited like any bottle's, with agent
`bottle-architect`.

### For the console

The console's launch page asks the host to propose a bottle for a task (PRD
0117, design repo; `suggest_link.py`). `bottle_suggest` (`task`, optional
`cwd`) -> `bottle_suggestion` (`job`, `state` `running`, or `error`) starts
the same flow as `bot-bottle bottles suggest`: `bottle-architect` in its
warm bottle, falling back to the rules path, validated against the host's
bottles ([bottles-suggest.md](bottles-suggest.md)). `cwd` is a project
directory on this host (an existing directory under the user's home, not the
home itself); with it the proposal sees the project's bottle, origin and
lockfiles, and without it the proposal says it had no project to look at. It
runs off the link, so the reply comes at once; at most four run at a time, and
one still running after 150 seconds is reported failed.
`bottle_suggest_status` (`job`) -> `bottle_suggestion` follows it: `state`
`running`, `done` with `proposal` (its `option`, the `bottles` it stacks, a
`new_bottle` name, extends and about, whether it `writes` a bottle, and the
`egress` hosts, `repos`, `env` names and `credentials` names and routes it
adds — never a secret's value — plus `similar`, `notes` and `source`,
`architect` or `rules`; and the guided start's review, PRD 0118: the `gist`,
the `new` grants, `connect` (each forge to connect first, with the `forge
connect` command to run here) and `edits`, each pending edit to another
bottle with its `bottle`, what it `adds`, `why`, the forges it `waits_on`
and its `diff`), or `error`; with `replies` (what was said so far) and
`decided` (each accepted edit, as written).
`bottle_suggestion_edit` (`job`, `decision` `accept` or `skip`) ->
`bottle_suggestion` decides the first pending edit: accepted, it's written
here as `bot-bottle start` writes it (a project bottle trusted, a host
bottle a new version, comments kept), and the proposal is validated again.
`bottle_suggestion_reply` (`job`, `reply`) -> `bottle_suggestion` asks the
architect again on the same job, the conversation replayed (at most eight
replies).
`bottle_suggestion_accept` (`job`, optional `name`) ->
`bottle_suggestion_accepted` (`bottles`, `written`, or `error`) writes the
bottle the host proposed, if it makes one, into the host's bottles as a new
version, and answers the bottles to launch with: the existing ones for a
reuse or stack, those plus the new one for additions, or just the new one.
It refuses while an edit is pending.
The console never sends a manifest, so only what this host proposed is
written. A finished job is kept ten minutes.

## GitHub through the bot-bottle app

The console connects github.com through the bot-bottle GitHub App's device
flow (ADR 0012, `github_connect.py`): `github_authorize_start` ->
`github_authorize` (`user_code`, `verification_uri`, `expires_in`, or `error`)
starts it; the host then polls GitHub itself and records the connection in
`forges.md` when the operator enters the code. `github_status` -> `github_status` follows it: the
app's `app` slug and `install_url`, `client_id_set`, the flow's `flow`
(`state` `idle`, `waiting` with the code, `connected` with `login`, or `error`)
`connected` (each user connected on github.com through the app: `login`,
`default`, and the app's `installations`
— `id`, `account`, `repository_selection` — or `error`), and `needs_install`
(authorized, but the app is installed nowhere). It asks GitHub each time, so
the console can poll it while the operator installs the app. The token never
reaches the console. An agent's `forge-connect`
for github.com gets a card with the install link and the `github_authorize`
option, never a token field; once GitHub is connected, the request is refused
with a note to use `add-repo`. An `add-repo` card for a repo the token can't
reach carries `install:`; one whose authorization is gone carries
`reauthorize:` and `github_authorize` beside the grant options. While either
is shown (or a `create-repo` card whose owner lacks the app, or its Administration permission, with `install:`), the host asks GitHub again every ten seconds for fifteen minutes
(`remote_actions.card_changed`) and raises the card again when that changes,
so it turns grantable once the app is installed.

## Workspace images

Agents often make a picture as a file and mention its path. The console's
"View" button asks the link for `workspace_file` (`agent_id`, `path`); the
reply `workspace_file` carries `content_type`, `data` (base64) and `size`,
or `error` (`workspace_files.py`). The host attaches to the running bottle
as for a snapshot and reads the file there through `Bottle.exec`, never from
a host path. It reads only absolute, normalized paths under `/home/node` or
`/tmp` with a png, jpg, gif, webp or svg extension. The guest resolves
symlinks (`readlink -f`), and the resolved path must pass the same checks,
there and again on the host. The content type comes from the extension,
never from the guest. Files over 10 MiB are refused. The console serves the
bytes with `nosniff` and a sandboxing CSP and shows them only through
`<img>`, so an SVG's script never runs.

## Uploading files to a running agent

The console's `+` button lets the operator upload a file into a running
agent's workspace. It asks the link for `put_workspace_file` (`agent_id`,
`path`, `data` base64); the reply `workspace_file_put` carries `path` and
`size`, or `error` (`workspace_files.py`). The host writes the file inside
the running bottle through `Bottle.exec`, never to a host path. Only
absolute, normalized paths under `/home/node/uploads` are accepted. The
guest checks the prefix again before writing. Files over 5 MiB are refused.
After the upload the console inserts the path into the message box so the
agent can be told about it.

Larger files go in chunks (`uploads.py`), so no single link message holds the
whole file:

- `upload_begin` (`agent_id`, `path`, `size`) → `upload_begun` with
  `upload_id`, `max_bytes` (the host's ceiling, 1 GiB) and `chunk_bytes`
  (4 MiB). It creates `<path>.partial-<id>` in the bottle.
- `upload_chunk` (`upload_id`, `seq`, `data` base64) → `upload_chunk_ack` with
  `received`. Chunks come in order (`seq` from 0), each at most `chunk_bytes`,
  never past the declared `size`; each is appended to the partial.
- `upload_end` (`upload_id`, `sha256`) → `upload_done` with the final `path`
  and `size`. The size and SHA-256 are checked here, over what was written,
  and in the guest when it has `sha256sum`; then the partial is moved into
  place without overwriting: a taken name becomes `name-1.ext`, `name-2.ext`…
- `upload_abort` (`upload_id`) → `upload_aborted`; the partial is removed.

An upload belongs to the agent that began it. At most 8 are in flight on a
host, and one with no chunk for 2 minutes is dropped with its partial. Any
step's refusal is its reply with `error`. The console waits for each reply
before the next chunk, and its own limit (the user's setting) is below the
host's.

## Renaming a bottled agent

A bottled agent's label is what people call it; its identity is what the
host keys everything by (state dir, VM, transcripts, deploy keys, commit
trailers), and a rename never touches the identity. The console asks the
link for `rename` (`agent_id`, `bottle`, `label`): a running agent's bottle
is the one the link runs it in, and a stopped one's is the `bottle` the
console names. The reply `renamed` carries the cleaned `label`, or `error`
(`relabel.py`). A label is trimmed, 1 to 128 characters, with no control
characters. The host writes it to the bottle's `metadata.json` and its
session record, so `bot-bottle bots`, the console's listings and a later
resume all use it. `bot-bottle rename <bottled agent> <label>` does the same
from the CLI; a console paired with the host keeps its own name until it's
renamed there.

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

### Audit records on the console

A console agent's conversation is also kept on the console as audit
records, readable only through this host and only once you unlock with a
passkey ([ADR 0013](../decisions/0013-audit-records-passkey-decrypt.md)).

| Module | Does |
|---|---|
| `audit_push.py` | each entry of a running console agent's conversation (the masked host copy), encrypted with AES-256-GCM under `<root>/audit.key` (created 0600 when first needed; never sent), bound to its run and position, and hash-chained to the agent's previous record across runs. Sent as `audit_record` at most 50 a tick, every 5 s. How far each agent got is in `<root>/audit-push/<agent>.json`, so a restart, resume or re-adopted bottle never sends an entry twice. Terminal-started agents aren't sent. |
| `audit_unlock.py` | `audit_unlock_challenge` (`session`) issues a single-use challenge (two minutes) for one console session; `audit_unlock` (`session`, `assertion`) checks the passkey's WebAuthn signature on this host (`webauthn.py`: ES256 and EdDSA, the console's origin and rp id, the challenge, the signature counter) and issues a token for that session: 12 hours, or an hour unused. |
| `audit_link.py` | `decrypt_audit_records` (`session`, `token`, `run_id`, `records`: `id`, `seq`, `ciphertext`; up to 200) -> `audit_decrypted`: only with a valid token, only for runs this host sent, at most 2000 records a minute, each read journalled as `audit.records_read`. |
| `audit_trust.py` | the passkeys trusted to unlock, in `<root>/audit-passkeys.json` (0600) with the origin and rp id they sign for. |

`bot-bottle pair` fetches the account's passkeys from the console
(`GET /api/v1/hosts/passkeys`) and trusts them only once you confirm their
fingerprints match the console's (Settings, Passkeys);
`bot-bottle audit passkeys [--trust]` lists or re-fetches them. The console
can't change them. Back up `audit.key`: without it the records can't be read.

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

**If the account is deleted** (or the host removed from it), the console no
longer knows this host's node id and closes the link as `unregistered host`
(`link_refusal.py`). The host only reports it: `status` shows the link as
refused, `doctor` warns and its `next:` is `bot-bottle pair --console-url
<url>`, home's status strip says to pair again, and the link asks only every
ten minutes instead of on its short backoff. It changes nothing on its own:
`console.json`, the iroh key and the link setting stay. `bot-bottle pair` then
pairs it like a new machine, to a new account or the same console, under the
same iroh key; the next accepted connection clears the refusal. Bottles,
agents and the local audit journal on the host aren't affected.

The console can list local agents, launch a **local agent by name** (with an
optional prompt, bottle names and label), stop one, and view or type into
sessions. Agent and bottle definitions always come from this host, never from
the console. A launch, resume or stop runs beside the link, after that agent's
previous one, so the console's other requests are answered while it waits on
the bottle. A resume of a bottle already running as that same agent (Resume
clicked twice after a Stop) answers `agent_started` again rather than an error;
a bottle running as another agent is refused.

A bottle remembers who started it: its `metadata.json` records `origin` and,
for the console, the console's `agent_id` (written once the agent runs, kept
when a resume rewrites the file). So `bot-bottle resume <bottle>` on the CLI
brings a console bottle back as **the same console agent**: the host takes the
id from the bottle's own metadata (a request can't name one) and records the
session as `console`; the link announces it with `agent_started`, and the
conversation keeps its id. A terminal bottle, or one recorded before this,
resumes as a terminal session, as before.

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

The console's launch page asks to `resolve` an agent (by name) in an ordered
list of bottles before it launches (`library.resolved_manifest`): the reply's
`text` (and `data`) is what that launch would run with, the bottles merged in
order as a launch merges them (none means the built-in `basic`), as one
bottle file's YAML. Its `elements` list each env var, egress route, repo and
forge with the file it comes from, as the Manifest tab's Pretty view does (a
later bottle wins a clash, so it's the source). Or `error` when it doesn't
resolve. Names are checked as a launch's are.

The console's Manifest tab asks for a running agent's `manifest`
(`manifest_view.py`).

Each bottled agent has manifest versions (`run_manifest.py`,
`state/<bottle>/manifest/`). Version 1 is its whole bottle chain flattened
into one document (`manifest/document.py`), recorded at launch. A resume adds
a version only if the files changed. Each launch and resume also records
`launch.json`: every bottle in the chain's fingerprint
(`manifest/fingerprint.py`: its full resolution, `extends` merged, in a
canonical order so key, route and repo order don't count, SHA-256) and the
host's bottle version then. The `manifest` reply's `bottle_versions` lists
the chain in order, each with whether it still resolves to what it launched
with (`latest`; a change to a bottle it extends counts) and, for a host
bottle, that version (`version`, `version_at`). A project's bottle (versioned
by its repo) and a built-in one have no version. The `manifest` reply's `current` is the
latest version with the supervisor's changes since: egress routes the live
policy has beyond the launch routes (`state/<bottle>/egress/routes.yaml`), and
repos granted while it runs. It also lists what can't be kept (a route
carrying a credential) and a digest. `manifest_save` writes it as the next
version if the digest still matches. With `bottle` (one of the reply's
`bottle_targets`: the launch's chain, then the host's other bottles) it also
writes the run's changes into that bottle. With `new_bottle` it writes a new
host bottle of that name instead, recorded as a version too and refused if
the name is taken. Without `extend` it's flattened: the whole chain, no
`extends`. With `extend` it extends the bottles it launched in and holds only the
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
the source "this host" (its connection) when nothing in the chain describes
it. Copying a granted repo into a bottle writes the forge's description with
it, never a token (ADR 0019).
`manifest_transfer` copies picked elements into a bottle, or moves them out of
the file they came from. A built-in bottle's elements, and this run's, are
copied. With `delete` it takes picked elements out of the file they come from
(the running agent keeps them until its next launch), and this run's out of
the running bottle. A built-in bottle's and the provider's stay. Each file
changed is saved as a new version.

The console's editable Bottle tab reads the agent's chain with `bottle_chain`
(`bottle_chain.py`): every bottle file the agent's bottle is built from, in
merge order (each pick's parents before it; a later one wins), each with its
`kind` (`host`, `builtin` or `project`), `writable` (host bottles only),
`digest`, `content`, and parsed `frontmatter` and `body`; plus this run's
additions (`run`) and the writable `targets`. `bottle_write` (`bottle`,
`digest`, and `frontmatter` with `body`, or raw `content`) writes one host
bottle of that chain: never a built-in or project bottle, nor a name outside
the chain. The host serializes data itself, refuses text that doesn't parse
or holds a secrets-file value, puts the file back if the agent's manifest no
longer loads, and records a version. A digest that no longer matches replies
`stale`. Changes apply from the next launch; the running bottle is
unaffected.
