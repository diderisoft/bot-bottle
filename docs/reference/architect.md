# Bottle architect

The bottle architect is an agent that drafts the bottle a task needs. You
describe the task, it proposes a bottle, and you review the proposal before
anything runs.

Its answer is always a typed proposal, never free YAML. bot-bottle checks it
against the same loader as any bottle, and drops what doesn't pass.

## Using it

| Command | Does |
|---|---|
| `bot-bottle start` | on a terminal, asks for an agent and a task, proposes a bottle, shows it for review, and launches in it |
| `bot-bottle start --auto "<task>"` | runs the proposal without a review, printing one line saying what it chose |
| `bot-bottle bottles suggest "<task>"` | prints the proposal and runs nothing |
| `bot-bottle bottles suggest --write NAME "<task>"` | saves the proposal as a host bottle named `NAME` |

The console's launch page proposes a bottle the same way.

An empty task skips the architect and runs the agent in `basic`.
`bot-bottle start --bottle <name>` never asks it.

### Reviewing

| Key | Does |
|---|---|
| `Enter` | runs it |
| `s` | saves it as a named bottle, then runs it |
| `e` | edits it as YAML in `$VISUAL` or `$EDITOR` |
| `d` | shows the details: each bottle it builds on, and why it adds each thing |
| `r` | replies: asks the architect again, with your reply and what you did |
| `c` | connects a forge its repos are on that this host hasn't connected |
| `a` / `x` | accepts or skips a proposed edit to another bottle |
| `q` | cancels |

A proposal that can't run on this host offers only edit, reply and cancel.

### Nothing is saved unless you ask

A proposal that adds anything runs as the run's own bottle, `suggested`
([ADR 0017](../decisions/0017-suggested-bottles-unsaved-unless-asked.md)).

- It's kept with the run. A resume finds it, the console shows it read-only,
  and the audit has its full text.
- It's never added to your bottles.
- `s` or `--write NAME` saves it like any host bottle, as a new version.

Edits to other bottles are written only once you accept each one. An accepted
edit to a project bottle is written in the repo, for you to commit, and
trusted on this host.

### `--auto`

`--auto` skips the review
([ADR 0018](../decisions/0018-start-auto.md)). Anything the bottle missed
comes to you as an approval at runtime. `BOT_BOTTLE_START_AUTO=1` makes it the
default, and `--no-auto` asks again for one run.

Without a terminal nothing prompts, so `--auto` is the only way to use a
proposal there. A proposal's edits to other bottles are then skipped.

## Where it runs

The architect runs in its own built-in bottle, `bottle-architect`, which holds
only its model API:

- the model key, injected by the gateway
- no repos
- no secrets
- no approval tools
- an empty workspace

That bottle is what contains it, so any harness can be the architect. Each
proposal is a fresh headless call, with a 120-second timeout, so tasks never
share a conversation.

The host controller keeps the architect's bottle running, so a proposal
doesn't wait for a boot. `BOT_BOTTLE_ARCHITECT_ALWAYS_ON=0` boots it on demand
instead, and stops it after `BOT_BOTTLE_ARCHITECT_IDLE` seconds idle (default
600).

### What it sees

- the task, and your replies
- your bottles: names, about text, `extends`, what each allows, and which
  secrets each needs that this host lacks
- the project's `origin` URL, its project bottle's name, and its lockfile
  *names*, never their contents
- the forges this host connected, and known ones it hasn't
- secret *names* from the secrets file, never values

## Customizing it

`bot-bottle setup` writes the architect to
`~/.bot-bottle/agents/bottle-architect.md`. Edit it like any agent:

- its body is the prompt
- `agent_provider.harness` picks the harness
- `model:` picks the model

Only your own copy is read, never a project's.

## What it may propose

A proposal reuses or stacks your bottles, adds to them, or starts a new one.
bot-bottle prefers them in this order:

| Option | Means |
|---|---|
| `reuse` | run in existing bottles as they are |
| `stack` | stack existing bottles |
| `stack+additions` | stack them, plus a few hosts, repos, env names or credentials |
| `new-extending` | a new bottle that extends existing ones |
| `new` | a new bottle |

It can also propose edits to other bottles, such as adding the project's repo
to `./main`, so later runs have it too.

### Constraints

Every proposal is checked. What fails a check is dropped with a note, rather
than failing the whole proposal.

- **No pass-through routes.** Every proposed route is inspected.
- **No secret values.** A credential is a secret's *name* and the host it goes
  to. The gateway adds the value at launch.
- **No secrets in env.** An env name that looks like a secret (`TOKEN`,
  `SECRET`, `PASSWORD`, `API_KEY`, …) is dropped, with a note to add it as a
  credential instead.
- **It loads.** The proposal is turned into a bottle and loaded with the real
  manifest loader, stacked as a launch would stack it.
- **Nothing the stacked bottles already allow.** A later route replaces an
  earlier one for the same host, so a repeat could narrow what a base bottle
  allows.
- **Grants only on known forges.** A repo grant is kept when this host
  connected its forge, a bottle describes it, or it's the project's `origin`.
  Otherwise it becomes a note.
- **Only secrets this host has.** A proposal that needs a secret this host
  lacks drops that part. When it can't do without it, it's marked
  **can't run**, and neither the review nor `--auto` runs it.
- **Edits are checked before they're offered.** An edit never touches a
  built-in bottle or an agent, and must load as if accepted.

## When the architect can't answer

A rules path answers instead, and the proposal's notes say why: no agent file,
a harness not logged in, a timeout, or an answer that wasn't a proposal.
`bottles suggest --rules` uses it on purpose.

The rules path never calls a model and ignores the task. It takes the first
that fits:

1. the project's `./main`, if it's trusted
2. `basic`, plus write access to the project's `origin`, when this host
   connected its forge
3. `basic`
