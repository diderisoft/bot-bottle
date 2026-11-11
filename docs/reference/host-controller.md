# Host controller

The host controller is the per-host daemon behind every bottle operation.
The CLI, the orchestrator and remote consoles all go through it
(PRD 0092, design repo). Its code is in `control/hostctl/`.

It runs as the user and adds no privilege. It gives every caller one API,
and it is not a security boundary. Its routes are in
[runtime-contract.md](runtime-contract.md#host-controller-as-the-orchestrator-sees-it).

## Running

The CLI starts the host controller on demand. You can also run it directly:

```
python -m bot_bottle.control.hostctl
```

- It binds `127.0.0.1`, on `BOT_BOTTLE_HOSTCTL_PORT` or an ephemeral port.
- It answers on its control socket, `~/.bot-bottle/hostctl/control.sock`
  (0600, in a 0700 directory). Asked for `status`, it replies with its
  `{pid, port, build_id}`. That is how the CLI finds it.
- It checks, migrates and backs up the host database when it starts,
  without asking ([Host database](#host-database)).
- The CLI starts the daemon under a lock, so concurrent CLIs start only one.
- The daemon holds `~/.bot-bottle/hostctl/daemon.lock` (`flock`) for its
  whole life. A second one for the same root exits at once (status 78),
  before it touches the log, the host database or the control socket.
- It logs to `~/.bot-bottle/hostctl/hostctl.log`, whichever of the CLI,
  systemd or launchd started it. At 16 MiB the daemon moves it to
  `hostctl.log.1` and opens a fresh one, keeping 3 older files.

### Restarts after an upgrade

The daemon's `build_id` is a hash of the package's `.py` files. The CLI
restarts a daemon whose `build_id` differs from its own.

| Case | What happens |
|---|---|
| The old daemon holds no bottles | The CLI restarts it. |
| It holds bottles, launch from a terminal | The launch asks first: "moves N running bottles to the new version". |
| It holds bottles, headless or non-terminal launch | The launch is refused. |
| It holds bottles, any other command | The command keeps using the old daemon. |
| `bot-bottle up --restart` | Restarts it on purpose, with the same question. |
| `bot-bottle up --restart --yes` | Answers the question, for scripts and services with no terminal. Without a terminal or `--yes`, the restart is refused. |

`up --restart` leaves a current orchestrator and gateway running.
`--restart-infra` reboots them too (see
[backends](backends.md#gateway-restarts)).

The daemon also refuses a launch itself once the code on disk no longer
matches its `build_id`. So no path, the phone included, runs a launch on
mixed code.

An upgrade under a running daemon is reported before a launch fails:

- `bot-bottle status` and `bot-bottle doctor` print "restart needed
  (installed version changed)" with the `up --restart` fix. The installer
  runs `doctor`.
- The console link's `host_stats.about` carries `restart_needed`.

Nothing restarts the daemon on its own.

### Listeners for the guests

The host controller adds one listener per backend, where that backend's
orchestrator guest reaches the host. It never binds `0.0.0.0`.

| Backend | Listens on |
|---|---|
| docker | the publish bridge |
| macos-container | the control network's vmnet gateway |
| firecracker | the orchestrator TAP |

Each listener accepts only peers on its link. It accepts only the guest
roles, `orchestrator` and `gateway`.

### As a service

`bot-bottle install` can add a launchd LaunchAgent or a `systemd --user`
unit. A crash is restarted. A clean exit is not.

When the CLI finds no daemon answering, it has the service manager restart
the unit (`systemctl --user restart`, `launchctl kickstart -k`). A unit that
runs but doesn't answer is replaced, not left to time out.

Stopping the daemon stops only its own process (`KillMode=process`,
`AbandonProcessGroup`). So agents outlive a restart. Units installed before
this need `bot-bottle install` again.

## Jobs

Launch, teardown and orchestrator bring-up run as jobs. Clients stream a
job's output from `GET /jobs/{id}/events`.

- Each job runs in its own thread.
- Its output is captured as events: `log` lines, then one `result` or
  `error`.
- Jobs are held in memory. They're dropped `RETENTION_SECONDS` after they
  finish.

A launch job prepares the plan without prompting, since the CLI has already
asked everything. It then hands the plan to a holder thread. That thread
stays inside `backend.launch(plan)` for the bottle's life, and tears the
bottle down when the agent exits.

A launch the host's budget can't fit is refused before it boots (see
[Bottles' budget](#bottles-budget)).

## Sessions

Each agent runs in tmux, on bot-bottle's own tmux server, never the user's.
Secrets never reach tmux's argv or environment.

- The pane runs `launch.py`. It fetches the real command and env once, over
  the control socket (`localsock.py`).
- tmux's `pane-died` hook runs `notify.py` to report the exit. The session's
  finisher then runs, and tmux detaches every viewer.
- Session records are rows of the host database's `sessions`, so a
  restarted daemon can take the sessions back. A record holds no label: the
  bottled agent's row keeps it.

### After a daemon restart

A restarted daemon re-adopts live sessions. It finishes the ones that died
while it was down. Without a record, `bot-bottle cleanup bots` removes the
bottle.

It then re-takes what running bottles hold in its process
(`BottleBackend.hold_adopted`). On Firecracker, that is the TAP slot lock of
each agent VM. Each VM's VMM runs in its own session for this reason.

A re-adopted session has no holder thread. So at launch the holder records how
to finish the bottle in its `bottled_agents` row (`backend/adoption.py`). It
holds no secrets:

- the container name
- the registry id
- the workspace layout
- deploy-key references

When an adopted session ends, the daemon:

1. saves the agent's work
2. tears the bottle down through `BottleBackend.adopted_bottle`
3. revokes its deploy keys, with tokens from the secrets file

A token that was only in the launching CLI's environment can't be found
then. That key is reported for revoking by hand.

### Status bar

The session's tmux status bar shows pending requests, blocked requests and
launch warnings. It sits outside the agent's pane, where the agent can't
fake or hide it.

**Requests.** The host controller polls the environment's orchestrator for
pending supervise proposals. It shows a one-line summary in the bar.

- The orchestrator is the one the environment's last bring-up recorded (see
  [architecture.md](architecture.md#components)).
- Each proposal is tagged with that orchestrator's URL. The terminal popup
  and the phone send the answer back there.

**Blocks.** The bar shows the gateway's block events as the audit journal
writes them, through `HostAudit.listen`. The design is PRD 0099 (design
repo).

- Each block shows for five seconds as `⊘ <host> · <reason>`.
- A burst shows the newest, with `(+N)`.
- A notice stays up at most ten seconds. Then the bar shows five seconds
  without one, so a retry loop can't keep it up.
- The session's counts by reason stay beside it, as a letter and a number:
  `A12 S3 P1`.
- Counts live in memory. They reset when the session or the daemon
  restarts.

| Letter | Reason (`reason_code`) |
|---|---|
| A | not on allowlist (`allowlist`) |
| R | outside the route's `methods` or `allow`, or git over HTTPS (`route`) |
| S | a secret in the request (`secret`) |
| I | an unsafe response (`inbound`) |
| M | a malformed request (`request`) |
| D | you denied it (`denied`) |
| P | git-gate rejected a push (`push`) |
| C | bottle config: a token unset, an unattributed request (`config`) |
| ? | an event without a code |

The console gets the same blocks, for the shield on the agent's page (see
[Blocks](#blocks)).

**Launch warnings.** A launch's warnings stay on the session as
`@bb_warning`. Examples are an isolation gap or a stale image.

- They show in amber, before the detach key.
- They're shortened to the first warning's first clause and a count of the
  rest.
- They take up to 70 columns, or 30 when the client is under 100 columns.
- A pending request takes their place.

**Width.** The bar fits each client's width (`#{client_width}`). The
notice's reason and the detach hint need 150 columns. Under 100 columns,
the request summary is cut.

### Viewers

| Viewer | Code | Transport |
|---|---|---|
| local terminal | `cli/commands/attach.py` | `tmux attach` |
| WebSocket | `attach.py` | binary frames for terminal bytes, JSON text frames for control |
| remote console | `console_terminal.py` | an iroh stream |

Every non-local viewer starts read-only. It can type only if the agent's
manifest sets `remote.writable`.

The starter agents that `bot-bottle setup` writes set it, so a paired phone
can talk to them. An agent written by hand is view-only unless it says so.

## Transcripts

The host controller copies each session's harness transcript to the host as
it's written (PRD 0095, design repo). The copy is kept exactly as read,
unparsed and unmasked.

pi, Claude and Codex declare a transcript (`AgentProvider.transcript_glob`).
A provider that doesn't isn't followed.

Each copy is kept at:

```
~/.bot-bottle/transcripts/<session>/<activation>/<file>.jsonl
```

The file is 0600, in 0700 directories. Its row in the host database
(`transcript_files`) holds:

- the guest path
- the offset read
- `start`: the guest offset the copy starts at, for a resumed run's copy
- the console agent id: the console's for a console launch, otherwise
  `terminal_agent_id`

Entries are parsed when read, by `AgentProvider.parse_transcript`. The
console reads them masked, over the link (see
[Reading transcripts](#reading-transcripts)).

| Audit event | When |
|---|---|
| `transcript.captured` | A copy's size and running SHA-256. Every five minutes while it grows, when a restarted daemon resumes it, and when it closes (`final: true`). |
| `transcript.rewritten` | A guest file shrank, or its bytes before the offset changed. |
| `transcript.deleted` | Retention deleted a run's copies. Lists each copy's size and SHA-256. |

`transcript.captured` marks its payload `agent-claimed`. The harness writes
the file inside the bottle, so it's the agent's account, not evidence. The
guest path is under `untrusted`.

**Rewrites.** When a guest file shrinks, or its bytes before the offset
change, its copy is kept as it was. The file is copied again from its start
to `<file>.rewrite-<n>.jsonl`.

**Retention.** A run's copies are deleted once the run is closed and
unchanged for the host's retention. That is 30 days by default. Set it
with:

```
bot-bottle audit transcript-retention [DAYS]
```

The check runs at start and daily after. A run's rows go with its copies.
They outlive the bottled agent, as the copies do, so the console can read a
stopped agent's conversation.

**After a restart.** A restarted daemon resumes each re-adopted session's
copies from the saved offsets. It execs through
`BottleBackend.attach_adopted`, which never tears the bottle down. Copies
of sessions that ended while it was down close with what they have.

### Details

At launch, a follower notes the files already matching the glob. Those are
another session's, and they're never copied. It records them with the run
(`transcript_runs`): the glob, those files, the agent id and the harness.

A resume keeps the bottle's home, and its harness continues its last
conversation. Claude's `--continue`, for example, appends to the file it
was writing.

- For a resume, the follower notes each file's size instead, in the run's
  row as `start`.
- What the agent adds to a file is copied from there.
- The earlier run's copy holds the rest.

Polling:

- The follower polls once a second, and once more after the agent exits.
- Each poll is one `exec` into the bottle. It reads each new file's new
  bytes by offset, up to 4 MiB a poll, plus the 64 bytes before the offset.
- A quiet transcript is polled less often, doubling up to every 5 seconds.
- New bytes, or the console typing into the agent, bring it back to every
  second.
- That `exec` gives up after 60 seconds. So a wedged one can't hold the
  follower, or the teardown waiting to close it.

The poll's script, its paths and offsets, goes as an argument, not on stdin.
On macOS every `container exec --interactive` leaks a pipe in the bottle's
runtime ([apple/container#2439](https://github.com/apple/container/issues/2439)).
Polled each second, that ran the host out of open files.

The daemon's other background `exec`s are bounded too. These are reading a
bottle's secret and exporting a workspace's changes.

`bot_bottle/transcript.py` holds the entry type the parsers share. The
parsers are in `contrib/<harness>/transcript.py`. A tree-shaped transcript,
as pi's and Claude's are, has an active branch.
`AgentProvider.active_entries` chooses it.

## Bottles' budget

The budget sets what bottles may use on this host:

```
bot-bottle host budget --memory GB --disk GB
```

- 0 clears a limit. No flags shows them.
- It's stored in the host DB.
- It's read at each launch and each `host_stats` report, so a change
  applies at once.
- `bot-bottle host caps` is its deprecated old name.

### Memory is allocation

A bottle is given 2 GiB unless its `resources:` say otherwise (see
[backends.md](backends.md#memory-and-vcpus)).

| Backend | What is set |
|---|---|
| firecracker | the VM's memory |
| macos-container | the container's `--memory` |
| docker | the agent's `mem_limit` |

The session record keeps what each bottle was given, as `memory`. A bottle
recorded before `resources:` counts at 2 GiB.

Allocated memory is the sum over the bottles running. That counts bottles
that are held, adopted after a restart, launching, or with a running
session.

A VM usually uses less than it's given. So allocation can sit well above
the host-wide figure.

### Storage is bot-bottle's own

Storage counts bot-bottle's own directories, plus what the container
runtimes hold for it. A background thread measures it every five minutes.
Launches read the last result and never measure.

The directories are the state root and the artifact and firecracker caches.
They count in allocated blocks, so a sparse VM disk counts what it holds.

Every runtime on the PATH is asked too:

| Runtime | Asked through |
|---|---|
| Docker | `docker system df -v` |
| Apple `container` | `container image list`, and its store under `~/Library/Application Support/com.apple.container` (`containers/`, `volumes/`) |

| Category | Holds |
|---|---|
| `vm_disks` | firecracker run dirs, the infra VMs' disks, kept `home.ext4` disks, and this environment's Docker or `container` containers' writable layers and volumes, by its resource prefix |
| `transcripts` | transcripts, audit, logs |
| `images` | the rest of the caches, the build root, and the runtimes' `bot-bottle-*` images |
| `workspaces` | the rest of the state root |

Limits of the count:

- A runtime image's unique bytes count. The layers it shares count once,
  at the largest share. So runtime images are an estimate.
- Docker's build cache isn't attributable, and isn't counted.
- The caches and runtime images are shared by every environment on the
  host.
- A runtime that can't be asked is listed in `budget.storage.unmeasured`
  as `"docker: <why>"`. It isn't counted as zero.

### Launch admission

A launch is checked against the budget after it's prepared and before it
boots. One launch is admitted at a time, so two launches can't both take
the last room.

| Case | Result |
|---|---|
| The bottle's memory doesn't fit | Refused: "<label> needs 2 GB of memory; 0 GB left in the 8 GB budget. Stop a bottle (`bot-bottle bots`) or raise the budget (`bot-bottle host budget --memory …`)." |
| bot-bottle's storage is over its budget | Refused, naming `bot-bottle cleanup images` to free cached images. |
| Storage at 90% of the budget or more | Goes ahead with a warning. |
| No budget set | No check. |

A refusal fails the launch job. The console sees `agent_errored` with the
message. An admitted bottle counts as launching until its holder takes it.

## Forwarded host logins

The host controller keeps a forwarded Claude or Codex login fresh in the
bottles that use it. A bottle opts in with
`agent_provider.login: host`.

The login is read once at launch. The gateway injects it, and the agent
holds a placeholder. The host's own CLI keeps refreshing its login, so the
host controller re-reads it:

| Trigger | How often |
|---|---|
| The login file changed | checked every five seconds |
| A timer | every five minutes |

**The file watch.** The host's CLI revokes the old access token the moment
it rotates its login. The timer alone left bottles on a revoked token
(`401 OAuth access token has been revoked`) for up to five minutes.

- The host controller watches the files each provider names
  (`AgentProvider.host_login_paths`).
- Those are Claude's `~/.claude/.credentials.json` and Codex's `auth.json`.
- When one changes and reads whole, the bottles forwarding that harness's
  login are refreshed at once, through the same path. A half-written file
  waits for the next look.
- The timer stays as the backstop. It is all a login kept outside a file
  gets, such as one in the macOS Keychain.

**The push.** A value that changed is pushed to the orchestrator that
registered the bottle (`POST /bottles/<id>/secret`). That is the
environment's recorded orchestrator. The gateway reads the value per
request.

- A push that orchestrator can't take fails, naming it. It can be down,
  reject the key, or not know the bottle.
- An environment brought up before bring-ups recorded an orchestrator has
  none recorded. Only then does the host controller try each listening
  orchestrator in turn.
- It treats a 401/403, a 404 or no answer as "not this one". It fails only
  when none accepted, listing each URL and why.

Codex's placeholder `auth.json` in the guest carries the real token's
expiry. So it's rewritten when that moves.

### Details

This covers bottles the host controller launched and those it adopted after
a restart. The adoption record keeps which egress slots carry the login,
which is nothing secret. The bottle's `ENV_VAR_SECRET` is read back from
inside it.

A host login that's missing, expired or within six hours of expiring is
logged once, with what to run on the host. It's audited as
`credential.renew_failed`. A refresh is audited as `credential.renewed`.

bot-bottle doesn't spend the host's refresh token itself (#769).

## Bottle suggestions

Every bottle suggestion runs in the host controller. It keeps the bottle
architect's own bottle warm (see
[bottles-suggest.md](bottles-suggest.md#bottle-architect-default)).

The CLI asks over HTTP, with the `cli` role only:

| Request | Body | Does |
|---|---|---|
| `POST /suggest` | `task`, optional `cwd`, `rules`, `settings`, and `turns` | Starts a `bottle.suggest` job whose `result` is `{"proposal": ...}`. |
| `POST /architect/warm` | `settings` | Boots the warm bottle in the background and answers its `state`: `booting`, `ready`, `down` or `unavailable`. |

`turns` is the conversation so far, after a reply.

The warm bottle:

- By default the host controller boots it when it starts, and keeps it up
  (`BOT_BOTTLE_ARCHITECT_ALWAYS_ON`). It has 512 MiB and one CPU.
- Without always-on, `bot-bottle start` sends the warm request as soon as
  it starts. The bottle stays up `BOT_BOTTLE_ARCHITECT_IDLE` seconds after
  its last suggestion, 600 by default.
- Either way, the host controller tears it down when it stops.
- It replaces one removed from under it on the next suggestion.
- Its start and stop are audited like any bottle's, with agent
  `bottle-architect`.

The console starts the same flow over the link (see
[Bottle suggestions from the console](#bottle-suggestions-from-the-console)).

## Host database

The host controller is the only writer of the host database,
`~/.bot-bottle/db/bot-bottle.db`
([architecture.md](architecture.md#databases)). The CLI never opens it. It
asks over HTTP, with the `cli` role only:

| Request | Body | Does |
|---|---|---|
| `GET /host/config` | none | The host's settings: `audit_delivery`, `audit_forward`, `firehose`, `transcript_retention_days`, `host_budget`, `github_app`. |
| `POST /host/config` | any of those settings | Sets the ones given, all or none, and answers with every setting. A bad value is 400. |
| `POST /audit/query` | the `audit query` filters | Indexes new journal records, then answers `{"records": [...]}`. |
| `POST /audit/rebuild` | `{}` | Re-creates the audit index. Answers `{"indexed": n}`. |
| `POST /audit/import` | `{"segment": <absolute path>}` | Imports a segment as its own chain. Answers `chain`, `records`, `extended` and `already_imported`. |
| `GET /forges` | none | `{"forges": [...]}`: each connection with its users. |
| `POST /forges` | `{"forge", "user", "make_default"}` | Records the user connected on the forge. Answers `{"forge": ...}`. A malformed connection is 400. |
| `POST /forges/remove` | `{"host", "user"?}` | Disconnects the user, or the whole forge, and drops their tokens from the secrets file. Answers `{"removed": [...]}`. |
| `GET /trust` | none | `{"trusted": [...]}`: the trust records, oldest first. |
| `POST /trust` | a record: `hash`, `bottle`, `path`, `grants`, `trusted_at` | Keeps it, replacing the record for its path. Answers like `GET`. |
| `GET /pairing` | none | `{"pairing": ..., "link": ...}`: the pairing, with its tokens' secret names, and the link's last state. |
| `POST /pairing` | `{"changes": {...}, "new": bool}` | Sets the fields given. A new pairing clears the rest. Token values go to the secrets file. Answers like `GET`. |
| `GET /audit/passkeys` | none | The trusted passkeys: `origin`, `rp_id`, `passkeys`. |
| `POST /audit/passkeys` | the same shape | Trusts exactly those, keeping a passkey's counter where the same key was trusted. Answers like `GET`. |
| `GET /agent-prefs` | none | `{"default", "dirs"}`: the remembered agent and each directory's last one. |
| `POST /agent-prefs` | `{"agent", "dir"}` | Remembers the agent for the directory, or as the remembered one when `dir` is `""`. Answers like `GET`. |
| `GET /infra/state` | none | `{"state": {...}}`: what infra bring-up recorded. |
| `GET /bottled-agents` | none | `{"bottled_agents": [...]}`: every bottled agent this host records. |
| `POST /bottled-agents` | `{"metadata": {...}}`, or `{"identity", "changes": {...}}` | Records a launch, or changes a bottled agent's `label`, `origin`, `agent_id` or `parent`. A label is checked as a rename checks it. Answers `{"bottled_agent": ...}`, null when nothing records it. |
| `POST /bottled-agents/remove` | `{"identity"}` | Deletes its state dir, snapshots and rows together. Answers `{"removed": bool}`. |

A journal that doesn't chain, or a segment that won't import, is 409.

These commands use the routes: `audit query`, `export`, `rebuild`,
`import`, `delivery`, `transcript-retention`, `retention`, `forward` and
`firehose`,
`host budget`, `forge connect`, `list`, `show` and `remove`, `trust`,
`pair`, `audit passkeys`, `start --dry-run`, `bots`, `resume`, `rename`,
`commit`, `cleanup bots`, a bare `bot-bottle`, `agents default`, the
guided `start` and `forge connect github --app`. Each starts the host
controller if it isn't running. `status`, home and `doctor` read the pairing
only from a running one. `audit verify` reads only the journal's files.

What infra bring-up recorded (`infra_state`) is read only from a running host
controller, never starting one. Bring-up runs in the host controller, its only
writer. `down`, `doctor`, `version` and `resolve-canary` read it this way.

Code the CLI shares with the host controller reads forge connections, trust
records, bottled agents and the GitHub app the same way (`host_access.py`). In the host
controller it uses the tables directly.

### Checks and backups

The host controller won't start on a damaged host database.

```
bot-bottle host restore-db --list     # the backups, newest first
bot-bottle host restore-db [BACKUP]   # put one back: the newest, or the one named
bot-bottle host restore-db --yes      # without asking first
```

| When | What happens |
|---|---|
| It starts | `PRAGMA quick_check` on the file. On a failure it exits (status 78), naming the newest backup and when it was taken. Its systemd unit doesn't restart it. |
| It starts, and the check passes | It migrates the file. Any table no host store owns is dropped, such as an older version's audit index or, on Docker, the orchestrator's old tables. |
| It answers on its control socket, and `index.db` is missing or fails the same check | In the background, it deletes `index.db` and rebuilds it from the journal. Audit queries and the prune wait for the check. `index.db` has no backups. |
| It has migrated, and every hour after | If the newest backup is a day old, or there is none, a new one: a `VACUUM INTO` copy in `~/.bot-bottle/db/backups/` (directory 0700, files 0600). It keeps the newest 3. |
| `host restore-db` | Refused while the host controller runs: it takes the daemon's lifetime lock, so none starts meanwhile. It says what comes back and asks first. Then it checks the backup, keeps the replaced file beside it as `bot-bottle.db.replaced-<time>`, and puts the backup in its place. |

Changes since the backup are lost on a restore. Trust records, audit
passkeys and forge connections come back as the backup has them, including
ones removed since. Backups stay on this host.

## Audit

The host controller is the host's only audit writer. Gateway events reach
it through the orchestrator (`POST /audit/append`).

The producers are:

- the host controller itself: `lifecycle.*`, `hostctl.*`
- the orchestrator
- the gateway's `egress`, `git-gate` and `supervise`

| Module | Does |
|---|---|
| `audit_journal.py` | JSONL segments in `~/.bot-bottle/audit/` (directory 0700, files 0600), which the writer only appends to. Each record hash-chains to the one before it across segments and restarts. An exclusive directory lock enforces one writer. Credential-shaped keys are refused and token-shaped values scrubbed before writing. `id` is the idempotency key, checked against the current segment. ([Segments and retention](#segments-and-retention)) |
| `audit_retention.py` | prunes old segments past the host's retention, at start and daily after |
| `audit_index.py` | a SQLite index of the journal in `~/.bot-bottle/db/index.db`, for `bot-bottle audit query`. It stops at the first record that doesn't chain. ([The index](#the-index)) |
| `audit_verify.py` | offline chain check (`bot-bottle audit verify`). Reports `truncated-tail`, `unreadable`, `edited`, `gap` and `chain-break`, and for the local journal `unrecorded-prune`. |
| `audit_forward.py` | at-least-once forwarding to an HTTPS endpoint, as native NDJSON or OCSF. The cursor advances only on a 2xx. |
| `audit_ocsf.py` | maps a journal record to OCSF 1.9.0. `restricted` records ship only the envelope and chain fields. |
| `audit_import.py` | stores segments from another host verbatim under `imported/<host>/<chain>/`, never joined to the local chain |

A journal that can't be written is logged, and the launch or teardown still
goes ahead. Where a bottle sets `audit.delivery: required`, egress and
git-gate refuse the action they couldn't record (see
[gateway.md](gateway.md)).

### Segments and retention

A segment closes at 64 MiB, and on the first record of a new UTC day. The
next record opens a new one, named for its UTC start time, so names sort in
chain order. The chain carries on across segments.

```
bot-bottle audit retention [--days N] [--max-gb N]
```

The journal keeps every segment until a retention is set. `0` means no limit.
The host controller prunes at start and daily after:

- It drops only whole segments, oldest first, never the current one.
- A segment the index hasn't read, or while forwarding is set the forwarder
  hasn't sent, stays.
- First it appends `journal.pruned`. That names the dropped segments, the
  first and last dropped records (`epoch`, `seq`, time), their size, the
  last one's hash, and each dropped segment's last hash
  (`segment_last_hashes`).

The oldest kept record's `prev` is that last hash. A crash partway through
the deletes leaves it one of the segments' hashes instead. `audit verify`
starts the chain from the `journal.pruned` record that names it. A local journal starting
anywhere else is `unrecorded-prune`, a break. An imported chain may start
anywhere.

### The index

The audit index lives in `~/.bot-bottle/db/index.db`, not the host
database. It is derived from the journal, so it is never backed up.

- Each query first indexes records written since the last one.
- `audit rebuild`, or a missing or damaged `index.db` at start, replays the
  journal and the imported chains.
- A rebuild covers only the kept segments. The `journal.pruned` records say
  what went before them.
- A prune waits for the index to catch up, as does a query during a rebuild.

### What the chain proves

The chain catches accidental damage and a partial edit. It does not stop
someone with the host user's access from rewriting it.

- Each record's `hash` is a plain SHA-256 over the previous hash and the
  record, with no key. Nothing is signed or MACed.
- Append-only is how the writer behaves, not something the OS enforces.
  The files are ordinary files owned by the host user.
- `audit verify` catches a changed, dropped, inserted or reordered record.
- Anyone with the host user's access can rewrite the whole chain from the
  edited record onward, and it verifies clean.
- Whole records cut from the end of the newest segment also leave a valid,
  shorter chain.

Tamper evidence against that needs a copy off the host:

```
bot-bottle audit forward --url <https endpoint> [--secret <name>] [--format native|ocsf]
```

- The host controller POSTs each record, within seconds of writing, to a
  receiver you run elsewhere.
- The receiver then holds the chain as it was when sent.
- Records already sent can't be rewritten from the host.
- Whoever controls the host can still change or stop what hasn't been sent
  yet.
- Forwarding is off until you set it. `--off` stops it.

### Audit records on the console

A console agent's conversation is also kept on the console as audit
records. They're readable only through this host, and only once you unlock
with a passkey ([ADR 0013](../decisions/0013-audit-records-passkey-decrypt.md)).

| Module | Does |
|---|---|
| `audit_push.py` | encrypts and sends each conversation entry as `audit_record` |
| `audit_unlock.py` | checks a passkey's signature and issues an unlock token |
| `audit_link.py` | decrypts records for a session with a valid token |
| `audit_trust.py` | keeps the passkeys trusted to unlock |

**Sending.** Each entry of a running console agent's conversation is sent.
The entry is the masked host copy.

- It's encrypted with AES-256-GCM under `<root>/audit.key`. The key is
  created 0600 when first needed, and never sent.
- Each record is bound to its run and position.
- Each is hash-chained to the agent's previous record, across runs.
- At most 50 are sent a tick, every 5 s.
- How far each agent got is kept in the host database (`audit_push_cursors`,
  and `audit_push_runs` for each run's next record). So a restart, resume or
  re-adopted bottle never sends an entry twice.
- An agent with no cursor on this host sends nothing until the console says
  where its chain ends. The host asks `audit_head` (`agent_id`), at most once
  a minute. The console answers `audit_head` with the agent's `runs` from this
  host (`run_id`, `next_seq`: its highest `seq` plus one) and its `last`
  record (`run_id`, `seq`, `ciphertext`, `chain_hash`), or null for none.
- The host decrypts that record for the transcript position it carried. It
  never resets a cursor itself, which would send every entry again or none.
- A run this host keeps for another agent is ignored. A `last` outside the
  answer's runs, or that doesn't decrypt, is no answer, and the host asks
  again.
- `bot-bottle status` counts the agents still waiting on an answer, on its
  console link line.
- Terminal-started agents aren't sent.

**Unlocking.**

- `audit_unlock_challenge` (`session`) issues a single-use challenge for one
  console session. It lasts two minutes.
- `audit_unlock` (`session`, `assertion`) checks the passkey's WebAuthn
  signature on this host.
- The check (`webauthn.py`) covers ES256 and EdDSA, the console's origin and
  rp id, the challenge, and the signature counter.
- It then issues a token for that session. The token lasts 12 hours, or an
  hour unused.

**Decrypting.** `decrypt_audit_records` takes `session`, `token`, `run_id`
and `records`, up to 200. Each record is `id`, `seq` and `ciphertext`. The
reply is `audit_decrypted`.

- It needs a valid token.
- A record decrypts only with this host's key, bound to its run and `seq`.
  One that doesn't gets its own `error`. A run sent before the host database
  was new still decrypts.
- It allows at most 2000 records a minute.
- Each read is journalled as `audit.records_read`.

**Trusted passkeys.** They're kept in the host database (`audit_passkeys`),
with the origin and rp id they sign for and each one's signature counter.

- `bot-bottle pair` fetches the account's passkeys from the console
  (`GET /api/v1/hosts/passkeys`).
- It trusts them only once you confirm their fingerprints match the
  console's, under Settings, Passkeys.
- `bot-bottle audit passkeys [--trust]` lists or re-fetches them. Trusting a
  passkey again keeps its counter.
- The console can't change them.

Back up `audit.key`. Without it the records can't be read.

## Firehose

The firehose fans the gateway's per-request records out to
`bot-bottle audit tail` subscribers and to configured sinks. Nothing is
stored.

| Sink | Gets |
|---|---|
| `exec` | a command that gets JSON lines on stdin, restarted with backoff |
| `https` | NDJSON batches, with a bearer token from the secrets file |

- Each consumer has its own bounded queue.
- A consumer that falls behind drops its oldest records, and is told how
  many.
- When nothing is consuming, the gateway stops building records.
- Settings are re-read every `RELOAD_SECONDS`.

## Remote access

With `bot-bottle pair` or `pair on`, the host controller dials out to the
console over iroh. It accepts no inbound connections
([ADR 0007](../decisions/0007-remote-access-terminates-in-host-controller.md)).

The console is the first of:

1. the one `--console-url` or `BB_CONSOLE_URL` names
2. the one this host last paired with
3. the hosted console at `https://bot-bottle.app` (`login.DEFAULT_CONSOLE_URL`)

`setup` and `bootstrap` offer to pair with the same one.

- The host dials the console it logged in to, over ALPN `bb-console/1`.
- It refuses a console whose node id isn't the one pinned at login.
- Pairing keeps the console's URL, this host's id on it, the pinned console
  and whether the link is on in the host database (`pairing`).
- The console's tokens for this host are in the host secrets file, as
  `BB_HOST_CONSOLE_ACCESS_TOKEN` and `BB_HOST_CONSOLE_REFRESH_TOKEN`, names no
  bottle can read.
  The `pairing` row names them.
- The iroh key stays a key file, `~/.bot-bottle/remote.key`.
- The link writes its state to `console_link`, for `bot-bottle status`, home
  and `doctor`. They ask a running host controller and start none.
- `pair`, `pair --password`, `pair on` and `off`, and token refreshes all
  change the pairing through the host controller (`POST /pairing`).

### What the console can reach

The console can list local agents, launch one by name, stop one, and view
or type into sessions. A launch takes an optional prompt, bottle names and
label.

- A launch names an agent and bottles this host already has.
- Agent content sent with a launch is refused.
- Bottles are not read-only to the console. It can edit host bottles and
  the secrets file (see
  [What pairing lets the console do](#what-pairing-lets-the-console-do)).
  A later launch runs with those edits.

A launch, resume or stop runs beside the link, after that agent's previous
one. So the console's other requests are answered while it waits on the
bottle.

A resume of a bottle already running as that same agent answers
`agent_started` again, rather than an error. That happens when Resume is
clicked twice after a Stop. A bottle running as another agent is refused.

### Console agents and terminal sessions

The console sees **every session on the host**, not only the ones it
launched. Each session record carries its `origin`: `console` or
`terminal`.

The link presents each running `terminal` session with `agent_present`.

- Its agent id is derived from its bottle and activation, as `t-…`. So it
  keeps the same id across reconnects and host controller restarts.
- The link sends this on every connect, and within a couple of seconds of a
  session starting.

After that, the session is one of the link's agents like any other:

- Its requests are raised on the phone.
- Its terminal is relayed. Typing still follows the agent's
  `remote.writable`.
- Its history and audit journal can be read.
- The console can stop it.

When the terminal and the phone both answer a request, the first answer
stands (see [gateway.md](gateway.md#supervise)).

A bottle remembers who started it. Its `bottled_agents` row records `origin`.
For the console, it also records the console's `agent_id`, written once the
agent runs and kept when a resume rewrites the row.

So `bot-bottle resume <bottle>` on the CLI brings a console bottle back as
**the same console agent**:

- The host takes the id from the bottle's own metadata. A request can't
  name one.
- It records the session as `console`.
- The link announces it with `agent_started`, and the conversation keeps
  its id.

A terminal bottle, or one recorded before this, resumes as a terminal
session, as before.

### If the account is deleted

When the account is deleted, or the host removed from it, the console
closes the link as `unregistered host`. The console no longer knows this
host's node id.

The host only reports it:

- `status` shows the link as refused.
- `doctor` warns. Its `next:` is `bot-bottle pair --console-url <url>`.
- Home's status strip says to pair again.
- The link asks only every ten minutes, instead of on its short backoff.

It changes nothing on its own. The pairing, the iroh key and the link setting
stay.

`bot-bottle pair` then pairs it like a new machine, under the same iroh
key. That can be to a new account or the same console. The next accepted
connection clears the refusal.

Bottles, agents and the local audit journal on the host aren't affected.

### What pairing lets the console do

Pairing gives the console operator-level control over this host's bottles,
their egress policy and the host secrets file. That console is the hosted
one at `https://bot-bottle.app` unless you paired with another.

Trust it as you trust your own terminal. A compromised console, or a stolen
console session, can do what follows.

`bot-bottle pair off` turns the link off. The host controller drops it
within seconds. `bot-bottle pair on` turns it back on. A host that never
paired has no link.

Through the link the console can:

- **Run agents.** Launch, resume and stop agents. Type into their sessions
  where the agent is `remote.writable`. Read their conversations and
  terminal history. Upload files into a running bottle.
- **Answer supervise requests.** Allow egress. Connect a forge with a token
  it sends, through `forge_connect`. Grant repos (`add_repo_answer`). Create
  repos and launch child bottles.
- **Widen a running bottle's egress** without a request from the agent.
  `operator_egress_allow` (`host`, optional `keep`) adds a default route for
  a host the bottle was blocked from.
  - The route is read-only unless it injects a credential.
  - With `keep`, it's written into the bottle file too.
- **Write host bottles.** Host bottles run as written, with no trust prompt
  ([manifest.md](manifest.md#trust)). So these edits apply at the next
  launch.
  - `bottle_write` replaces a host bottle in a running agent's chain with
    content the console sends.
  - `manifest_save` and `manifest_transfer` write a run's changes or picked
    elements into a host bottle or a new one.
  - `bottle_write` can't write a built-in or project bottle.
  - `manifest_transfer`, a kept approval and an accepted suggestion edit
    can change a project's bottle. The host then trusts it, and leaves it
    for you to commit.
  - All of these are described under
    [Console link protocol](#console-link-protocol).
- **Use the host secrets file.** List the names in it with
  `list_secrets` -> `secrets_listed`, names only. Set a value in it with
  `set_secret` (`name`, `value`) -> `secret_set`. The value is never echoed
  in the reply or a log line. A reserved `BB_HOST_` name is refused.

Together these let a console point a route's credential at a host of its
choosing. So a compromised console can send a host secret off the host
through a bottle.

There is no action that runs a command on the host itself. What the
console starts runs in a bottle.

What it can't do:

- **Read a secret's value over the link.** `list_secrets` returns names.
  `set_secret` only writes. Replies and logs never carry a value. A bottle
  edit can still send one to a host of its choosing, as above.
- **Launch agent content of its own**, or write a built-in bottle.
- **Read the encrypted audit records it holds without a passkey-signed
  unlock** (see [Audit records on the console](#audit-records-on-the-console)).
  - The key, `audit.key`, never leaves this host.
  - A decrypt needs a token. Only a WebAuthn assertion from a passkey this
    host trusts can get one.
  - The trusted passkeys are set on the host at `bot-bottle pair`, after
    you confirm their fingerprints. The console can't change them.
  - This covers the stored records only. Conversations and terminal output
    read live over the link reach the console readable. Transcripts have
    host secret values and token shapes masked.
- **Connect to the host.** The link is dial-out only, to the node id pinned
  at login.

## Console link protocol

The console and the host controller exchange these messages over the link.
An arrow shows a request and its reply.

| Message | Section |
|---|---|
| `host_stats` | [Host stats](#host-stats) |
| `metrics_watch` -> `bottle_metrics` | [Live metrics](#live-metrics) |
| `agent_working` | [Working indicator](#working-indicator) |
| `blocked` | [Blocks](#blocks) |
| `resize` | [Terminal relay](#terminal-relay) |
| `transcript`, `transcript_appended`, `transcript_search` | [Reading transcripts](#reading-transcripts) |
| `snapshot` | [Snapshots](#snapshots) |
| `workspace_file` | [Workspace images](#workspace-images) |
| `put_workspace_file`, `upload_*` | [Uploading files to a running agent](#uploading-files-to-a-running-agent) |
| `rename` -> `renamed` | [Renaming a bottled agent](#renaming-a-bottled-agent) |
| `github_authorize_start`, `github_status` | [GitHub through the bot-bottle app](#github-through-the-bot-bottle-app) |
| `bottle_suggest`, `bottle_suggest_status`, `bottle_suggestion_*` | [Bottle suggestions from the console](#bottle-suggestions-from-the-console) |
| `library` | [Library](#library) |
| `resolve` | [Resolving a launch](#resolving-a-launch) |
| `manifest`, `manifest_save`, `manifest_transfer` | [Manifest tab](#manifest-tab) |
| `bottle_chain`, `bottle_write` | [Bottle tab](#bottle-tab) |
| `audit_record`, `audit_head`, `audit_unlock_challenge`, `audit_unlock`, `decrypt_audit_records` | [Audit records on the console](#audit-records-on-the-console) |
| `list_secrets`, `set_secret`, `operator_egress_allow` | [What pairing lets the console do](#what-pairing-lets-the-console-do) |

### Host stats

`host_stats` reports this host's storage and memory, all in bytes. It's sent
on each connect and every 60 seconds.

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

It covers the host as a whole, and the bottles against their budget (see
[Bottles' budget](#bottles-budget)).

| Field | Means |
|---|---|
| `storage` | the filesystems holding the state root and the caches, each counted once |
| `memory` | what the kernel reports in use: `MemTotal - MemAvailable` on Linux, free and inactive pages on macOS |
| `cap` | repeats the budget, for consoles that predate the `budget` block |
| `budget.*.budget` | 0 means none is set |
| `budget.storage` | left out until the first storage walk after the daemon starts |
| `about.restart_needed` | true once bot-bottle was upgraded under the running daemon, which refuses launches until `bot-bottle up --restart` |

**`about`** is what the host's console page says about it:

- its OS: `macOS`, or a Linux's `NAME` from `/etc/os-release`
- the backend its last bring-up recorded, `""` before one
- this install's version and short commit, as `bot-bottle version` prints
  them

**Bottle CPU and memory.** Each running bottle's `cpu_pct` and `mem_used`
(bytes) are left out when they can't be read. CPU % is a share of the
bottle's CPUs, 0-100.

- A Firecracker VM's share is of its vCPUs, from its `config.json`. One
  busy vCPU of two is 50%.
- A container's is its `stats` figure, capped at 100. Bottles set no
  container CPU limit to scale it by.

**`forge_alerts`** are forge tokens that stopped working for good. There is
one per credential, oldest first. `key` is the host secret's name.

| `kind` | Raised when |
|---|---|
| `github_reauthorize` | The GitHub authorization can't be renewed: its refresh token expired or was revoked. Raised by the git-gate token renewer. |
| `gitea_token_refused` | The forge answered 401/403 when a launch, grant or teardown used the token. |

- Each is raised once per failure state.
- It's cleared when the credential works again, or is replaced with
  `forge connect`.
- Network trouble is only a warning.
- Alerts live in the host controller's memory. After a restart, the next
  renewal or launch raises them again.

### Live metrics

`metrics_watch` sets which bottles the console's live gauge watches. Its
`bottles` list of bottle ids replaces the link's watched set. An empty list
stops it.

- While the set is non-empty, the link samples the watched bottles it has
  running. It does this in one pass every 3 seconds, on every backend.
- It sends `bottle_metrics`: `at`, and `bottles` with `id`, `cpu_pct`,
  `mem_used` and `memory`.
- The interval leaves room for a container's `stats` call, which takes 1-2 s
  each.
- A pass still running when the next is due skips it.
- The set belongs to the connection, capped at 32. A reconnecting console
  sends it again.
- The live samples keep their own CPU baseline. So the minute `host_stats`
  figure still covers its whole minute.

### Working indicator

`agent_working` tells the console whether a running agent is working on a
prompt. It's sent when that changes, and once per agent on each connect.

The harness decides from the newest lines of its copy
(`AgentProvider.working`):

| Harness | Working |
|---|---|
| Claude | starts with a prompt or a tool call, ends at `turn_duration` or a reply that ends the turn |
| Codex | from `task_started` to `task_complete` or `turn_aborted` |
| no markers (the default) | while its copy grew within the last 20 seconds |

It's the agent's own output. So it's an indicator for the console only,
never a security decision.

### Blocks

The link sends the gateway's blocks to the console, for the shield on the
agent's page. They're alerts, not requests. Nothing waits on them.

Every couple of seconds the link sends `blocked` per running agent, host
and reason. Repeats are merged with a `count`, at most 20 at a time.

| Field | Holds |
|---|---|
| `agent_id` | the agent |
| `host` | the blocked host |
| `reason` | the code from the [status bar](#status-bar) table |
| `at` | when |
| `detail` | for a secret or a rejected push only: the gateway's own words, which name the detector, never the value |

A block never carries a path or a body, which can hold the secret. A
connection that opens gets the last five minutes' blocks. The audit journal
keeps the record.

### Terminal relay

The console's relay is one tmux client per agent. The console's `resize`
sizes it, to the most recent viewer's terminal.

The link keeps the last size asked for each running agent. It opens every
new relay at that size. Otherwise a relay reopened after a broken stream
would start at tmux's default, and a terminal of a different size draws its
output garbled.

### Reading transcripts

The console reads a conversation over the link. Entries are masked before
they leave the host. The host's copy itself stays as read.

| Message | Does |
|---|---|
| `transcript` | returns a page of the agent's entries, on the harness's active branch |
| `transcript_appended` | tells the console when more landed |
| `transcript_search` | finds the newest entries containing a query, optionally of given kinds, with a snippet around each match |

Masking uses the egress scanner's `redact_tokens`. It masks token patterns,
and every value in the host's secrets file in its common encodings.
Entries are parsed when read, then masked.

Search is case-insensitive, over the masked text. So a query can't confirm
a secret.

**How a page is built.** Each session file is put together from its copies,
across the bottle's runs, in the order they were made. A `/clear` starts a
new session file.

- A resumed run's copy continues at its `start`.
- A rewrite starts the file over.
- An entry's `seq` is its place in its file, plus a stride per earlier
  session. So it never moves, or names another entry.

**Which entries a page covers.** A page is from the current session, since
the last `/clear`. `first` and `last` bound it. These read the whole history
instead:

- a cursor, `before` or `around`, into an earlier session, such as a search
  hit from before a `/clear`
- `all: true`
- a current session with nothing in it yet

`scope` says which was read, `current` or `all`. Seqs are the same in both.

### Snapshots

The console's snapshot button asks the link for `snapshot`. Restoring is
not built yet.

1. The host attaches to the running bottle through its adoption record
   (`BottleBackend.attach_adopted`).
2. It takes the copied git workspace's patch since launch. That is the patch
   teardown saves.
3. It keeps it as
   `~/.bot-bottle/snapshots/<session>/<activation>/<taken>.patch` (0600).
4. A row of the host database's `snapshots` points at it. It holds when,
   the conversation's newest `seq` then, files and lines changed, size and
   SHA-256.
5. Each is audited as `snapshot.taken`.

A workspace that isn't a git copy can't be snapshotted. Removing the
bottled agent deletes its snapshots, rows and patches.

### Workspace images

Agents often make a picture as a file and mention its path. The console's
"View" button fetches it with `workspace_file`.

- The request is `agent_id` and `path`.
- The reply `workspace_file` carries `content_type`, `data` (base64) and
  `size`, or `error`.

The host attaches to the running bottle as for a snapshot. It reads the
file there, through `Bottle.exec`, never from a host path.

- It reads only absolute, normalized paths under `/home/node` or `/tmp`.
- The extension must be png, jpg, gif, webp or svg.
- The guest resolves symlinks with `readlink -f`. The resolved path must
  pass the same checks, there and again on the host.
- The content type comes from the extension, never from the guest.
- Files over 10 MiB are refused.

The console serves the bytes with `nosniff` and a sandboxing CSP. It shows
them only through `<img>`, so an SVG's script never runs.

### Uploading files to a running agent

The console's `+` button uploads a file into a running agent's workspace.
After the upload, the console inserts the path into the message box, so the
agent can be told about it.

A small file goes in one message, `put_workspace_file`.

- The request is `agent_id`, `path` and `data` (base64).
- The reply `workspace_file_put` carries `path` and `size`, or `error`.
- The host writes the file inside the running bottle through `Bottle.exec`,
  never to a host path.
- Only absolute, normalized paths under `/home/node/uploads` are accepted.
  The guest checks the prefix again before writing.
- Files over 5 MiB are refused.

Larger files go in chunks, so no single link message holds the whole file:

| Request | Reply | Does |
|---|---|---|
| `upload_begin` (`agent_id`, `path`, `size`) | `upload_begun` with `upload_id`, `max_bytes`, `chunk_bytes` | creates `<path>.partial-<id>` in the bottle |
| `upload_chunk` (`upload_id`, `seq`, `data` base64) | `upload_chunk_ack` with `received` | appends the chunk to the partial |
| `upload_end` (`upload_id`, `sha256`) | `upload_done` with the final `path` and `size` | checks the upload and moves it into place |
| `upload_abort` (`upload_id`) | `upload_aborted` | removes the partial |

- `max_bytes` is the host's ceiling, 1 GiB. `chunk_bytes` is 4 MiB.
- Chunks come in order, with `seq` from 0. Each is at most `chunk_bytes`,
  and never past the declared `size`.
- At `upload_end`, the size and SHA-256 are checked over what was written.
  They're checked in the guest too, when it has `sha256sum`.
- The partial is then moved into place without overwriting. A taken name
  becomes `name-1.ext`, `name-2.ext`…

Limits:

- An upload belongs to the agent that began it.
- At most 8 are in flight on a host.
- One with no chunk for 2 minutes is dropped, with its partial.
- Any step's refusal is its reply with `error`.
- The console waits for each reply before the next chunk.
- The console's own limit, the user's setting, is below the host's.

### Renaming a bottled agent

A rename changes a bottled agent's label, never its identity. The label is
what people call it. The identity is what the host keys everything by.

The identity covers the state dir, VM, transcripts, deploy keys and commit
trailers.

The console asks the link for `rename` (`agent_id`, `bottle`, `label`).
The reply `renamed` carries the cleaned `label`, or `error`.

- A running agent's bottle is the one the link runs it in.
- A stopped one's is the `bottle` the console names.
- A label is trimmed, 1 to 128 characters, with no control characters.
- The host writes it to the bottled agent's row, the one place it's kept.
  So `bot-bottle bots`, the console's listings, its session and a later
  resume all use it.

`bot-bottle rename <bottled agent> <label>` does the same from the CLI. A
console paired with the host keeps its own name until it's renamed there.

### GitHub through the bot-bottle app

The console connects github.com through the bot-bottle GitHub App's device
flow (ADR 0012). The token never reaches the console.

**Connecting.** `github_authorize_start` starts the flow. Its reply,
`github_authorize`, carries `user_code`, `verification_uri` and
`expires_in`, or `error`. The host then polls GitHub itself. It records the
connection in the host database when the operator enters the code.

**Status.** `github_status` answers `github_status`. It asks GitHub each
time, so the console can poll it while the operator installs the app.

| Field | Holds |
|---|---|
| `app`, `install_url` | the app's slug and install link |
| `client_id_set` | whether a client id is set |
| `flow` | the flow's `state`: `idle`, `waiting` with the code, `connected` with `login`, or `error` |
| `connected` | each user connected on github.com through the app: `login`, `default`, and the app's `installations` (`id`, `account`, `repository_selection`), or `error` |
| `needs_install` | authorized, but the app is installed nowhere |

**Cards.**

- An agent's `forge-connect` for github.com gets a card with the install
  link and the `github_authorize` option. It never gets a token field.
- Once GitHub is connected, that request is refused with a note to use
  `add-repo`.
- An `add-repo` card for a repo the token can't reach carries `install:`.
- One whose authorization is gone carries `reauthorize:`, and
  `github_authorize` beside the grant options.
- A `create-repo` card whose owner lacks the app, or its Administration
  permission, carries `install:`.

While any of those is shown, the host asks GitHub again every ten seconds,
for fifteen minutes (`remote_actions.card_changed`). It raises the card
again when that changes. So the card turns grantable once the app is
installed.

### Bottle suggestions from the console

The console's launch page asks the host to propose a bottle for a task. This
starts the same flow as `bot-bottle bottles suggest`.

That flow runs `bottle-architect` in its warm bottle, falling back to the
rules path. The proposal is validated against the host's bottles (see
[bottles-suggest.md](bottles-suggest.md)).

| Request | Reply | Does |
|---|---|---|
| `bottle_suggest` (`task`, optional `cwd`) | `bottle_suggestion` (`job`, `state` `running`, or `error`) | starts a suggestion |
| `bottle_suggest_status` (`job`) | `bottle_suggestion` | follows it |
| `bottle_suggestion_edit` (`job`, `decision` `accept` or `skip`) | `bottle_suggestion` | decides the first pending edit |
| `bottle_suggestion_reply` (`job`, `reply`) | `bottle_suggestion` | asks the architect again on the same job |
| `bottle_suggestion_accept` (`job`, optional `name`) | `bottle_suggestion_accepted` (`bottles`, `written`, or `error`) | writes the proposed bottle |

These actions take no manifest from the console. They write only what this
host proposed. Other console actions do write bottles and secrets from what
the console sends (see
[What pairing lets the console do](#what-pairing-lets-the-console-do)).

**`cwd`** is a project directory on this host. It must be an existing
directory under the user's home, not the home itself.

- With it, the proposal sees the project's bottle, origin and lockfiles.
- Without it, the proposal says it had no project to look at.

**Running.** The job runs off the link, so the reply comes at once.

- At most four run at a time.
- One still running after 150 seconds is reported failed.
- A finished job is kept ten minutes.

**Status.** `bottle_suggestion` carries `state` `running`, `done` with
`proposal`, or `error`. It also carries `replies`, what was said so far,
and `decided`, each accepted edit as written.

The `proposal` holds:

- its `option`, the `bottles` it stacks, and a `new_bottle` name, extends
  and about
- whether it `writes` a bottle
- the `egress` hosts, `repos`, `env` names and `credentials` names and
  routes it adds, never a secret's value
- `similar`, `notes`, and `source`, which is `architect` or `rules`

It also holds the guided start's review:

- the `gist`
- the `new` grants
- `connect`: each forge to connect first, with the `forge connect` command
  to run here
- `edits`: each pending edit to another bottle, with its `bottle`, what it
  `adds`, `why`, the forges it `waits_on`, and its `diff`

**Editing.** An accepted edit is written here as `bot-bottle start` writes
it. A project bottle is trusted, a host bottle gets a new version, and
comments are kept. The proposal is then validated again.

**Replying.** A reply replays the conversation to the architect. At most
eight replies are allowed.

**Accepting.** `bottle_suggestion_accept` writes the bottle the host
proposed, if it makes one, into the host's bottles as a new version. It
refuses while an edit is pending. It answers the bottles to launch with:

- the existing ones, for a reuse or stack
- those plus the new one, for additions
- or just the new one

### Library

The console's Library asks for the host's `library`. The console mirrors it
read-only, and keeps the last copy for while the host is offline.

- It holds each home agent and bottle under `~/.bot-bottle/`.
- It holds the built-in bottles a home one doesn't shadow.
- Each comes with its text, cut at 64 KiB.
- Project bottles aren't sent.

### Resolving a launch

The console's launch page asks to `resolve` an agent, by name, in an
ordered list of bottles before it launches. The reply shows what that launch
would run with (`library.resolved_manifest`).

- `text` and `data` are the bottles merged in order, as a launch merges
  them, as one bottle file's YAML.
- No bottles means the built-in `basic`.
- `elements` lists each env var, egress route, repo and forge with the file
  it comes from, as the Manifest tab's Pretty view does.
- A later bottle wins a clash, so it's the source.
- The reply is `error` when it doesn't resolve.
- Names are checked as a launch's are.

### Manifest tab

The console's Manifest tab asks for a running agent's `manifest`. The reply
shows the agent's manifest versions, what it runs with, and what changed in
this run.

**Versions.** Each bottled agent has manifest versions, under
`state/<bottle>/manifest/`.

- Version 1 is its whole bottle chain flattened into one document,
  recorded at launch.
- A resume adds a version only if the files changed.
- Each launch and resume also records `launch.json`. It holds every bottle
  in the chain's fingerprint, and the host's bottle version then.
- A fingerprint is a SHA-256 of the bottle's full resolution, with
  `extends` merged. It's in a canonical order, so key, route and repo order
  don't count.

**`bottle_versions`** lists the chain in order.

- Each says whether it still resolves to what it launched with, as
  `latest`. A change to a bottle it extends counts.
- A host bottle also carries that version, as `version` and `version_at`.
- A project's bottle is versioned by its repo, and a built-in one has no
  version.

**`current`** is the latest version, with the supervisor's changes since:

- egress routes the live policy has beyond the launch routes
  (`state/<bottle>/egress/routes.yaml`)
- repos granted while it runs

It also lists what can't be kept, such as a route carrying a credential,
and a digest.

**`elements`** lists everything the agent runs with. Each egress route,
repo, forge and env var is keyed `<kind>:<name>`, with its source.

- The source is the nearest bottle file in the chain that declares it. The
  others are listed too.
- The source can also be "this run" or "agent provider".
- A repo granted this run shows its grant.
- The forge it's on is listed with the source "this host", its connection,
  when nothing in the chain describes it.
- Copying a granted repo into a bottle writes the forge's description with
  it, never a token (ADR 0019).

**`manifest_save`** writes `current` as the next version, if the digest
still matches.

| Option | Effect |
|---|---|
| `bottle` | also writes the run's changes into that bottle, one of the reply's `bottle_targets`: the launch's chain, then the host's other bottles |
| `new_bottle` | writes a new host bottle of that name instead, recorded as a version too, and refused if the name is taken |
| no `extend` | the bottle is flattened: the whole chain, no `extends` |
| `extend` | it extends the bottles it launched in, and holds only the run's changes |
| `keys` | limits any of these to some of the run's changes: `egress:<host>`, `repo:<name>` |
| `remove` | takes those changes away from the running bottle instead |

- Adding a route for a host a bottle already has merges in the new matches
  (`approvals.with_routes`). So nothing it allowed is dropped.
- With `remove`, the bottle's live policy is rendered again with those
  routes as they launched, or without them. It's then set on the
  orchestrator.
- With `remove`, repos granted while it runs are revoked
  (`git_gate.live_grant.revoke_live`). A repo it launched with is refused.

**`manifest_transfer`** copies picked elements into a bottle, or moves them
out of the file they came from.

- A built-in bottle's elements, and this run's, are copied.
- With `delete`, it takes picked elements out of the file they come from.
  The running agent keeps them until its next launch.
- With `delete`, this run's elements are taken out of the running bottle.
- A built-in bottle's elements and the provider's stay.
- Each file changed is saved as a new version.

### Bottle tab

The console's editable Bottle tab reads the agent's chain with
`bottle_chain`. It writes one host bottle with `bottle_write`. Changes apply
from the next launch, and the running bottle is unaffected.

`bottle_chain` returns every bottle file the agent's bottle is built from,
in merge order. Each pick's parents come before it, and a later one wins.
Each file carries:

- `kind`: `host`, `builtin` or `project`
- `writable`, for host bottles only
- `digest` and `content`
- parsed `frontmatter` and `body`

It also returns this run's additions (`run`) and the writable `targets`.

`bottle_write` takes `bottle`, `digest`, and either `frontmatter` with
`body` or raw `content`. It writes one host bottle of that chain.

- It never writes a built-in or project bottle, nor a name outside the
  chain.
- The host serializes data itself.
- It refuses text that doesn't parse, or that holds a secrets-file value.
- It puts the file back if the agent's manifest no longer loads.
- It records a version.
- A digest that no longer matches replies `stale`.

## Code map

Where each part lives, for readers checking the code. Paths are under
`bot_bottle/control/hostctl/` unless shown otherwise.

| Part | Code |
|---|---|
| Running | `runner.ensure_running`, `runner.read_state`, `localsock.py`, `listeners.py`, `service_unit.py` |
| Host database | `host_db.py`, `host_state.py`, `bot_bottle/cli/host_state_client.py`, `bot_bottle/lib/db_store.py` |
| Jobs | `jobs.py`, `bottles.py` |
| Sessions | `sessions.py`, `launch.py`, `localsock.py`, `notify.py`, `bot_bottle/backend/adoption.py` |
| Status bar | `requests.py`, `blocks.py` |
| Transcripts | `transcripts.py`, `transcript_retention.py`, `bot_bottle/transcript.py` |
| Bottles' budget | `bot_bottle/backend/bottle_size.py`, `bot_bottle/backend/runtime_store.py`, `budget.StorageMeter`, `BottleService._launch` |
| Forwarded host logins | `credential_refresh.py`, `AgentProvider.refresh_credentials` |
| Bottle suggestions | `architect_bottle.py`, `suggest_link.py` |
| Firehose | `firehose.py`, `firehose_sinks.py` |
| Remote access | `remote.py`, `pairing.py`, `link_refusal.py` |
| Host stats | `host_stats.py`, `bot_bottle/forge_alerts.py`, `bottle_metrics.py` |
| Live metrics | `live_metrics.py` |
| Blocks | `block_link.py` |
| Reading transcripts | `transcript_serve.py` |
| Snapshots | `snapshots.py` |
| Workspace images and uploads | `workspace_files.py`, `uploads.py` |
| Renaming | `relabel.py` |
| GitHub | `bot_bottle/github_connect.py` |
| Library | `library.py` |
| Manifest tab | `manifest_view.py`, `bot_bottle/run_manifest.py`, `bot_bottle/manifest/document.py`, `bot_bottle/manifest/fingerprint.py`, `manifest_elements.py` |
| Bottle tab | `bottle_chain.py` |
