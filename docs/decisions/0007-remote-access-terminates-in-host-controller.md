# ADR 0007: Remote access over iroh terminates in the host controller

- **Status:** Accepted
- **Date:** 2026-10-23
- **Deciders:** didericis

## Context

Remote access (#478) needs a transport between each host and the console, so a
phone or browser can watch an agent and answer it. PRD 0070 chose iroh and
placed the host end in the orchestrator. Since then:

- The console implemented its side (`bb-console/1`) on the `iroh` Python
  bindings. Hosts dial it, and browsers and phones talk only HTTPS/WebSocket to
  the console.
- On Docker and macOS the orchestrator has no route to the internet, by design
  (no NAT'd path on Docker; the host-only control network on macOS). Only
  Firecracker's orchestrator VM is NAT'd.
- PRD 0092's host controller now owns launch, teardown and the tmux sessions
  with read-only/writable viewers. The orchestrator relays remote launches and
  attaches to it.

The spike in
[`docs/research/remote-access-iroh-transport.md`](../research/remote-access-iroh-transport.md)
compared the possible placements.

## Decision

1. **The host end of iroh runs in the host controller**, not the orchestrator.
   This supersedes the placement in PRD 0070.
2. **Dial-out only.** The host connects to the console it logged in to and
   pins that console's node id. It accepts no inbound iroh connections.
3. **Optional and off by default.** The `iroh` bindings ship as an optional
   install extra (`bot-bottle[remote]`), imported only when remote access is
   enabled. The package's default install stays stdlib-only.
4. **No Intel-macOS support.** Upstream publishes no Intel-macOS wheel since
   1.0, and the default macOS backend (Apple `container`) already requires
   Apple Silicon. We don't build or host our own wheel.
5. **Launches name local definitions.** In v1 a remote caller can launch only a
   bottle that already exists under `~/.bot-bottle/`, by name. Accepting bottle
   definitions from the console would cross the `$HOME` trust boundary, so it
   needs its own decision.

## Consequences

- **Good:** works the same on all three backends with no change to their
  network isolation, and no new per-host unit.
- **Good:** grants nothing new. The orchestrator's relay already lets a remote
  caller launch and attach through the host controller; this removes a hop.
- **Cost:** iroh's QUIC stack (Rust) parses network packets in a host process
  with the user's privileges, rather than in a guest. It only does so after
  the host has dialed out to a pinned console, and only when remote access is
  enabled. If the threat model (#337) rules that out, the fallback is a
  dedicated sandboxed unit with outbound-only egress.
- **Cost:** the console must bind each host's iroh node id to its device-flow
  approval and refuse unknown node ids, and return its ticket at login. Until
  it does, hosts shouldn't connect.
- **Cost:** remote access is unavailable on Intel Macs.

## Links

- [`docs/research/remote-access-iroh-transport.md`](../research/remote-access-iroh-transport.md)
- PRD 0070 (original placement), PRD 0092 (host controller), #478
