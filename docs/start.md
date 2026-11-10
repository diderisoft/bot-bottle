# Getting started

bot-bottle runs a coding agent in its own microVM or container. The agent
can do what it likes in there. It reaches the network only through a gateway
that holds the credentials, allows only the hosts its bottle names, and
checks what goes out.

## Requirements

Every host needs **git** and **tmux 3.1 or newer**. Python 3.11+ is used if
you have it; if not, the installer fetches one.

| Host | Backend | Needs |
|---|---|---|
| Linux with KVM | firecracker (microVMs) | `/dev/kvm` with your user in the `kvm` group, the [`firecracker`](https://github.com/firecracker-microvm/firecracker/releases) binary on `PATH`, e2fsprogs |
| Apple silicon Mac | macos-container | Apple's [`container`](https://github.com/apple/container) CLI. No Docker. |
| Anything else | docker | Docker, asked for with `--backend=docker` |

The **Docker fallback** is the weakest boundary. The agent runs in a
container that shares the host's kernel (under gVisor when Docker has
`runsc`), so a kernel bug is a way out. Use it for CI, or where neither of
the others runs. [backends.md](reference/backends.md) compares all three.

You also need an agent harness on the host: Claude Code or Codex, logged in,
or pi (setup can sign it in to a free OpenRouter model). bot-bottle uses that
login; the agent sees only a placeholder.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/diderisoft/bot-bottle/main/install.sh | sh
```

This installs the newest release and runs `bot-bottle bootstrap`, which:

1. **installs**: sets up this host's backend (the firecracker network pool,
   or the macOS host firewall), checks tmux, and adds autostart for the host
   controller. That setup has one step that needs root. It shows you the
   command and asks before running it; say no and it prints the commands
   instead.
2. **sets up** a starter agent for each logged-in harness it finds, then
   offers to pair your phone (optional; see below).
3. **checks** it all with `bot-bottle doctor`, which ends with a `next:` line:
   the one thing left to do on this host.

The installer doesn't install the backend itself (`container`, `firecracker`
or Docker); `doctor` says what's missing. Once it's there, run
`bot-bottle bootstrap` again. Everything about the install, including the Python and uv paths and
NixOS, is in [install.md](reference/install.md).

To remove it: `bot-bottle uninstall` (it keeps `~/.bot-bottle`; `--purge`
removes that too). See [Uninstall](reference/install.md#uninstall).

## First run

In your project's directory:

```sh
bot-bottle start
```

The first run pulls the release's images. Then:

1. **Pick an agent.** The first time, choose a harness. It remembers your
   choice; `bot-bottle start codex` picks another.
2. **Describe the task**, for example "fix the flaky test in tests/net".
   Leave it empty to run the agent in the built-in `basic` bottle (package
   registries only).
3. **Review the proposed bottle.** The bottle architect reads the task and
   the project and proposes a bottle sized to it: the hosts, repos and env
   it needs and no more. The review shows what it adds beyond the bottles it
   builds on. `d` shows the details, `e` edits it, `s` saves it as a named
   bottle, `q` cancels.
4. **Accept** with Enter. The agent starts in that bottle with your task as
   its first prompt.

A suggestion you don't save runs as that run's own bottle and isn't added to
your library. `bot-bottle start --auto "<task>"` runs the proposal without
the review. A bare `bot-bottle` opens a home menu for the same thing, plus
running agents and pending approvals. The details are in
[bottles-suggest.md](reference/bottles-suggest.md).

**Your code.** The agent reaches the repos its bottle grants through
git-gate. To copy the current directory in instead, add `--cwd`: when the
agent exits, its changes are saved as a patch under `~/.bot-bottle/work/`,
and bot-bottle prints the `git apply` line for it.

**Sessions.** Each agent runs in a tmux session owned by bot-bottle's host
controller, so it keeps running when your terminal closes.

- `Ctrl-]` detaches; the agent keeps working.
- `bot-bottle bots` lists bottled agents, running and stopped, by id;
  `bot-bottle attach <id>` joins one again.
- When the agent exits cleanly, its bottle is torn down and removed. A
  stopped one (`x` on it in the home menu's Agents list, Stop on the
  console, or an agent that exits with an error) keeps its home: its
  conversation and workspace. `bot-bottle resume` picks a stopped one and
  continues its last conversation.

## When the agent is blocked

When an agent needs something its bottle doesn't allow (a new host, a repo),
it asks through supervise, and the request waits for you. The session's
status bar shows it. Press `Ctrl-\` in the session to review it, or run
`bot-bottle supervise` from another terminal. A request the gateway holds
because it looks like it carries a secret is asked the same way.

You can also answer on your phone. Pairing links this host to the hosted
console at <https://bot-bottle.app>, where you can watch your agents and
answer their requests. It is optional; setup offers it once.

```sh
bot-bottle pair        # pair this host (prints a QR code)
bot-bottle pair off    # turn the link off
```

The host dials out to the console and accepts no inbound connections.
`--console-url` (or `BB_CONSOLE_URL`) pairs with another console. Details in
[host-controller.md](reference/host-controller.md#remote-access).

## Your own bottle

A bottle is a Markdown file with YAML frontmatter. This one gives an agent
the package registries, push access to one GitHub repo, and one API whose
token the gateway adds. `~/.bot-bottle/bottles/web.md`:

```markdown
---
extends: basic                       # package registries, downloads only
git-gate:
  repos:
    - github.com/you/app:write       # read-only without :write
egress:
  routes:
    - host: api.example.com
      inspect:
        auth: { scheme: Bearer, token_ref: EXAMPLE_API_TOKEN }   # injected; the agent never sees it
---

The `web` bottle: push to you/app, call api.example.com.
```

The repo grant needs this host connected to GitHub once:
`bot-bottle forge connect github`. Then run an agent in the bottle:

```sh
bot-bottle start claude --bottle web
```

Agents don't name a bottle; you pick one at launch, so the same agent works
in any bottle.

**Trust.** Your own bottles, under `~/.bot-bottle/bottles/`, run as written.
A bottle committed to a repo, at `.bot-bottle/bottles/<name>.md`, is named
`./<name>`, and runs on a host only once that host trusts its exact contents.
The trust prompt lists every host and secret it touches, including what it
inherits. Any change to the file asks again. `bot-bottle trust <name>`
reviews one ahead of time.

Every field is in [manifest.md](reference/manifest.md); more examples are in
[examples/](../examples/).

## Useful commands

| Command | Does |
|---|---|
| `bot-bottle` | the home menu |
| `bot-bottle start` | start an agent (guided) |
| `bot-bottle status` | the host controller, orchestrator, console link and running agents |
| `bot-bottle doctor` | check the host; ends with what to do next |
| `bot-bottle agents` | list agent definitions |
| `bot-bottle bottles` | list bottle definitions and their versions |
| `bot-bottle bots` | list bottled agents, running and stopped |
| `bot-bottle attach <id>` | join a running agent's terminal |
| `bot-bottle resume` | start a stopped bottled agent again |
| `bot-bottle supervise` | answer pending requests |
| `bot-bottle down` | stop the orchestrator, gateway and host controller |
| `bot-bottle help` | every command |

There is no `stop` command: stop a running agent from the home menu or the
console, or exit its harness.

## Next

- [Architecture](reference/architecture.md): how the pieces fit.
- [Manifest](reference/manifest.md): bottles, agents, trust and egress routes.
- [Threat model](security/threat-model.md): what bot-bottle defends against,
  and what it doesn't.
