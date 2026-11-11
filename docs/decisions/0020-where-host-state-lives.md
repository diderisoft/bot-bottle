# ADR 0020: Each kind of host state has one place to live

- **Status:** Accepted
- **Date:** 2026-11-10
- **Deciders:** didericis

## Context

Nothing says which kind of host state goes where, so each feature has picked
its own store. State is spread across a database, append-only logs, manifests,
a couple of dozen JSON files and the secrets file.

The results:

| Problem | Example |
| :-- | :-- |
| One name, two databases | The CLI and host controller open `<root>/db/bot-bottle.db`. The orchestrator opens its own copy in its VM (Firecracker) or volume (macOS). They share a file only on Docker. |
| Writers without coordination | The host database runs in rollback-journal mode, mostly without a busy timeout, and both the CLI and host controller write it. |
| JSON with several writers and no lock | Per-bottle `metadata.json` (CLI and host controller), `console.json` (login and pairing). |
| The same thing kept twice | Audit cursors in a table and in JSON files. The last-used agent in `start.json` and `state/quickstart.json`. |
| Machine state as a manifest | Only `forge connect` writes `forges.md`. It isn't versioned, and a hand edit can undo what connecting checked. |
| A second record of events | `supervise_audit_entries` repeats what the audit journal records. Nothing reads it, and it isn't tamper-evident. |
| State in the cache | `registry.ext4` holds the orchestrator's database but sits in `~/.cache`, which is meant to be re-downloadable. |

## Decision

Each kind of state has one store, picked by the test in its row.

| Store | Holds | The test |
| :-- | :-- | :-- |
| Authored content (files) | Bottles and agents (manifests) and skills. | Would a person write, review or commit it? |
| `bot-bottle.db` | Source-of-truth state that code writes: settings, connections, trust, cursors, queues, sessions, per-bottle metadata. | Is it written by code, read by several processes, or updated atomically, and would losing it lose something? |
| `index.db` | Indexes derived from logs, such as the audit index. | Can it be rebuilt from something else? |
| Append-only logs | The audit journal (hash-chained), transcripts, operational logs. | Does it only grow, and would editing it be a problem? |
| Secrets store and key files | Secret values and key material. | Is it a credential or a key? |
| Blobs | Disk images, root filesystems, committed images, workspace disks, patches. | Is it opaque bytes too big for a row? |
| Lock and runtime files | Locks, sockets, pid files, generated scripts, service units. | Does it exist only while something runs? |

Code writes authored content only on someone's behalf, such as a suggestion, a
console edit or a sync. It writes it only in a versioned directory.

### Three databases

Each database has one job and one writer.

| Database | Job | Writer |
| :-- | :-- | :-- |
| `bot-bottle.db` | The host's source of truth. | The host controller. The CLI goes through it. |
| `index.db` | Indexes rebuildable from logs. A missing or corrupt index is rebuilt. | The host controller. |
| `registry.db` | The orchestrator's working state: the bottle registry, encrypted egress secrets, the supervise queue, canary state. | The orchestrator, on every backend. |

- One helper opens every connection, in WAL mode, with a busy timeout.
- Migrations run when the database's writer starts.

### Rules

- **The journal is the only record of what happened.** Tables may index it or
  hold working state. What an event needs, such as a policy diff, goes into
  its journal record.
- **No secret values in `bot-bottle.db` or `index.db`.** They name secrets
  (`secret:NAME`). `registry.db` keeps a bottle's egress secrets encrypted, for
  the bottle's lifetime only.
- **Each table is portable or machine-specific.** Portable state, such as
  settings, forge connections and passkeys, could move to another machine. A
  table holding both marks each column.
- **Trust records are machine-specific.** Trusting a project bottle records
  this machine's forge connection (ADR 0019).
- **No JSON state.** JSON is only generated output for another program or
  process, written fresh and never read back.
- **`~/.cache` holds only what can be downloaded or rebuilt.** Anything else
  lives under the environment's root.
- **The audit journal and `hostctl.log` rotate.** Pruning old journal
  segments records a `journal.pruned` event with the dropped range and the
  last hash, so the chain stays verifiable. Transcripts are deleted after
  their retention.

### No migration

Moving state into its store comes with no code that reads the old files.

| Old state | On upgrade |
| :-- | :-- |
| Rebuildable (caches, indexes, infrastructure state) | Rebuilt. |
| Not rebuildable (forge connections, pairing, trust, passkeys) | Redone once, as [install.md](../reference/install.md#upgrading-to-this-release) says. |
| Bottled agents running or kept for resume | Can't cross the change. The curl installer refuses to upgrade while any exist. A `uv` or `pipx` install skips that check, so install.md says to stop them first. |

[install.md](../reference/install.md#old-files) lists the old files, and
`bot-bottle uninstall` deletes them.

## Consequences

- Placing new state is a lookup, not a choice. A reviewer can check a change
  against the store table.
- One writer per database, with WAL, removes lost updates and lock errors. The
  cost is that the CLI needs the host controller for host state.
- The host database stays small, so it's cheap to back up and check.
  `index.db` holds the large derived part and never needs a backup.
- Upgrading across the change means reconnecting forges, pairing again and
  re-trusting project bottles once, with no bottled agents running.
- The exceptions below break a rule for a named reason. None is a precedent.

### Exceptions

| State | Rule it breaks | Why it stays |
| :-- | :-- | :-- |
| git-gate's `state/<id>/git-gate/*.json` | No JSON state | The host controller reads them back to provision a bottle's git-gate again. Moving them into the registry is a follow-up. |
| A Firecracker VM's `config.json` | No JSON state | It's Firecracker's own format. It's read back for the VM's vCPUs, TAP and guest IP. |
| Host-wide Firecracker slice claims | State lives under the environment's root | They're lock files in `/var/lib/bot-bottle-fc/slices`, shared by every environment and user on the machine. No one environment's database can hold them. |

## Links

- [ADR 0019](0019-forges-are-described-in-bottles-connected-on-hosts.md):
  forges are described in bottles and connected on hosts. Its `forges.md`
  moves into `bot-bottle.db` under this record.
- [Architecture](../reference/architecture.md).
- PRD 0129 (design repo): state consolidation, the plan for moving each piece
  of existing state.
