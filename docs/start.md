# Getting started

bot-bottle runs a coding agent in its own VM. The agent can do anything
inside it. Everything it sends out goes through a gateway that holds your
credentials and only lets through what its bottle allows.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/diderisoft/bot-bottle/main/install.sh | sh
```

You need git, tmux 3.1+ and one of these:

| Host | Backend | Also needs |
|---|---|---|
| Linux with KVM | firecracker | the [`firecracker`](https://github.com/firecracker-microvm/firecracker/releases) binary |
| Apple silicon Mac | macos-container | Apple's [`container`](https://github.com/apple/container) CLI |
| anything else | docker | Docker. Weaker: the agent shares your kernel. |

The installer sets up the host and creates an agent for each harness you're
logged in to (Claude Code, Codex or pi). It asks before the one step that
needs `sudo`. It ends by telling you what, if anything, is left to do.

More: [install.md](reference/install.md). To remove it, run
`bot-bottle uninstall` ([details](reference/install.md#uninstall)).

## Run an agent

In your project:

```sh
bot-bottle start
```

1. Pick an agent.
2. Describe the task: "fix the flaky test in tests/net".
3. Review the bottle it proposes: the hosts, repos and secrets the task
   needs, and nothing else.
4. Press Enter. The agent starts on your task.

The agent reaches your code through the repos its bottle grants. To copy the
current directory in instead, add `--cwd`. When the agent exits, its changes
are saved as a patch under `~/.bot-bottle/work/`, with the `git apply` line
to use it.

The agent runs in the background. `Ctrl-]` detaches, `bot-bottle bots` lists
your agents, and `bot-bottle attach <id>` goes back in. `bot-bottle resume`
restarts a stopped one where it left off.

## When it's blocked

If the agent needs a host or repo its bottle doesn't allow, it asks you and
waits. Press `Ctrl-\` in its session to answer.

You can also answer from your phone:

```sh
bot-bottle pair
```

Scan the QR code and approve. This links the host to
[bot-bottle.app](https://bot-bottle.app). It's optional, and
`bot-bottle pair off` turns it off.

## Write a bottle

A bottle is a Markdown file. This one allows the package registries, pushes
to one repo, and calls one API with a token the agent never sees.

`~/.bot-bottle/bottles/web.md`:

```markdown
---
extends: basic
git_gate:
  repos:
    - github.com/you/app:write
egress:
  routes:
    - host: api.example.com
      auth: {type: bearer, value: secret:EXAMPLE_API_TOKEN}
---
```

Put the API token in the secrets file, `~/.bot-bottle/secrets`, as
`EXAMPLE_API_TOKEN=...`. Connect GitHub once, then run in it:

```sh
bot-bottle forge connect github
bot-bottle start claude --bottle web
```

A bottle committed to a repo (`.bot-bottle/bottles/`) only runs after you
trust it. The prompt lists every host and secret it uses, and any change asks
again.

More: [manifest.md](reference/manifest.md), [secrets.md](reference/secrets.md),
[examples](../examples/).

## Commands

| Command | |
|---|---|
| `bot-bottle` | home menu |
| `bot-bottle start` | start an agent |
| `bot-bottle bots` | list your agents |
| `bot-bottle attach <id>` | rejoin one |
| `bot-bottle resume` | restart a stopped one |
| `bot-bottle supervise` | answer pending requests |
| `bot-bottle status` | what's running |
| `bot-bottle doctor` | check the host |
| `bot-bottle help` | everything else |

## Next

- [How it works](reference/architecture.md)
- [What it defends against, and what it doesn't](security/threat-model.md)
