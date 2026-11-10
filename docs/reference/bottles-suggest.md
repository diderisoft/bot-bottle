# `bottles suggest`

`bot-bottle bottles suggest "<task>"` proposes the bottle a task needs before
it runs.

The output is always a typed proposal, never free YAML. So the manifest loader
can validate it and the pretty view can show what it contains.

## Usage

```
bot-bottle bottles suggest "<task>" [options]

Options:
  --cwd DIR        project directory to read (default: current directory)
  --write NAME     save the proposal as a new host bottle named NAME
  --rules          use the rules path only: no model, the same answer every time
  --json           print raw proposal JSON to stdout
```

The CLI, the guided `bot-bottle start` and the console's launch page run the
same flow (`suggest.flow`). `bottle-architect` answers first, and the rules
path answers when it can't.

### Example

```
$ bot-bottle bottles suggest --rules "deploy bot-bottle-console to Fly staging"
Run in basic.

option:  reuse
from:    rules
bottles: basic
note:    rules path: no trusted project bottle or connected forge; using basic

elements preview:
  egress   *.docker.io               any request  [basic]
  egress   *.ghcr.io                 any request  [basic]
  ...
```

With a connected forge and a project `origin`:

```
$ bot-bottle bottles suggest --rules "push a release"
Run in basic, adding 1 repo.

option:  stack+additions
from:    rules
bottles: basic
  + repo   git.example.com/acme/app:write  # write access to the project's own repo
note:    rules path: origin acme/app on git.example.com
```

## The guided `bot-bottle start`

On a terminal, `bot-bottle start` without `--bottle` suggests a bottle, shows
it, and launches in it. Bare `bot-bottle`, on a terminal, is a home menu that
starts this same flow (`commands/home.py`).

1. **Agent.** The agent you last ran, else a pick among the logged-in
   harnesses that have an agent ([details](#agent-step)).
2. **Task.** What the agent should do. Empty runs the agent in `basic`, as a
   plain start, with no suggestion.
3. **Suggest**, for the current project, with a spinner
   ([details](#suggest-step)).
4. **Review** the proposal: a short glance, with more on `d`
   ([keys](#review-keys)).
5. **Launch** in that bottle with the task as the agent's first prompt.

`bot-bottle start claude --bottle web` launches exactly as before. Ctrl-C
quits without launching.

**Nothing is saved unless you ask** (ADR 0017). A suggestion that adds
anything runs as the run's own bottle, `suggested`.

- It is kept with the run: a resume finds it, the console shows it read-only,
  and the audit has its full text.
- It is never added to the library.
- "Save it as a bottle" writes it like `--write`, versioned like any host
  bottle, and runs that.

### Review keys

| Key | Does |
|---|---|
| `Enter` | runs it |
| `s` | saves it as a named bottle first |
| `e` | edits it as YAML in `$VISUAL`/`$EDITOR` ([details](#editing-the-proposal)) |
| `d` | toggles the details. With an edit pending, it also shows its diff above them |
| `q` | cancels |
| `c` | connects a forge this host hasn't connected, on the screen ([details](#connecting-a-forge)) |
| `a` | accepts the first pending edit to another bottle and writes it then |
| `x` | skips the first pending edit |
| `r` | replies: takes a line and asks the architect again ([details](#replying)) |
| `↑`/`↓` | scrolls the hosts, repos and env vars under the details card |
| `esc` | backs out of connecting a forge |

`Enter` and `s` wait while a forge isn't connected. They also wait until every
edit is accepted or skipped.

A proposal that can't run on this host ([constraints](#constraints)) offers
only edit, reply and cancel.

### `--auto`

**`--auto`** (ADR 0018) skips the review. It prints one line saying what it
chose, and runs it, unsaved:

```
--auto: running in basic, adding egress api.open-meteo.com (from architect)
```

Anything the bottle missed goes through supervise approvals at runtime.
`BOT_BOTTLE_START_AUTO=1` makes it the default. `--no-auto` asks again for one
run.

**Without a terminal nothing prompts.** `--auto` is then the only way to use a
suggestion, and the task comes as an argument:

```
bot-bottle start --auto "fix the flaky test"
bot-bottle start codex --auto "…"
```

An agent can go first, as in the second line. Without `--auto`, give
`--bottle` (or `--headless`).

`--auto`, `--verbose` and `--dry-run` keep plain output, and nothing draws
without a terminal. Without the screen, a proposal's edits are skipped, with a
line saying so. The proposal is validated again without them.

## `--write NAME`

`--write NAME` saves the validated proposal as a new host bottle at
`~/.bot-bottle/bottles/<NAME>.md`. It is a new version in the bottles history.

It refuses a name that isn't a bottle name, or a bottle that already exists.
The console's "Use this bottle" writes the same way.

| Option | What it writes | How to launch it |
|---|---|---|
| `reuse`, `stack`, `stack+additions` | only the additions (egress routes, repo grants, env names), with **no `extends`** unless it has repo grants, in which case it extends the proposal's bottles | stacked after the proposal's bottles. The CLI prints the flags, e.g. `--bottle basic --bottle <NAME>` |
| `new-extending`, `new` | the additions plus `extends:` from `new_bottle.extends`, else the first proposed bottle, else `basic` | on its own: `--bottle <NAME>` |

A `reuse` or `stack` with no additions needs no new bottle. Launch with the
bottles it names.

An env name is written with a `?NAME` placeholder, prompted for at launch. The
body is the new bottle's about text and the proposal's notes.

## The proposal

A proposal is a JSON object with these fields:

| Field | Type | Description |
|---|---|---|
| `summary` | string | one plain sentence on what it recommends and why, for the person deciding ([more](#summary)) |
| `option` | string | `reuse`, `stack`, `stack+additions`, `new-extending`, or `new` (preference order) |
| `bottles` | array | existing bottles to use or extend |
| `new_bottle` | object or null | for `new-extending`/`new`: `{name, extends, about}` |
| `additions.egress` | array | routes to add: `{host, methods, why}` |
| `additions.repos` | array | repo grants: `{grant, why}`, e.g. `git.example.com/owner/repo:write` |
| `additions.env` | array | env var names: `{name, why}` — values come from the secrets file |
| `additions.credentials` | array | injected secrets: `{name, route, why}` — a secret name and the egress host it goes on |
| `edits` | array | changes to other bottles, each accepted or skipped on the review: `{bottle, extends, add, why}` ([more](#edits-field)) |
| `similar` | array | existing bottles close to the proposal: `{bottle, covers}` |
| `notes` | array | free-text caveats, including why the rules answered when the architect couldn't |
| `source` | string | which path produced it: `architect` or `rules` (set by the flow, never by the model) |

#### Summary

`summary` is asked for in the request, so architect prompts written before it
still give one.

It is cleared when validation or the runnability check drops part of the
proposal. The review then makes one from what's left.

#### Edits field

An edit's `bottle` is a host bottle, a project bottle (`./main`) or
`new:<name>`. `new:<name>` is a new host bottle, with `extends`.

`add` takes what `additions` does, plus `forges`. `forges` is
`{<host>: {kind, url}}`, the description of a forge its repos are on.

### Constraints

Proposals follow these rules.

- **`inspect: false` is never proposed.** Routes always allow TLS inspection.
  A user who needs pass-through edits the bottle.
- **Secret values are never included.** `additions.credentials` carries only a
  secret *name* from the host secrets file. The gateway injects the value at
  launch.
- **No secrets in env.** An env var's value is visible to the agent. So an env
  name that looks like a secret (`TOKEN`, `SECRET`, `PASSWORD`, `API_KEY`, …)
  is dropped, with a note to add it as a credential instead.
- **Grants on known forges.** A repo grant on a forge nothing describes becomes
  a note ([details](#grants-on-known-forges)).
- **Validation.** The proposal is turned into a bottle document and loaded with
  the real manifest loader. Any element that fails is dropped and noted,
  rather than failing the whole proposal.
- **Nothing the bottles already cover.** An addition the proposal's own
  bottles already allow is dropped ([details](#nothing-the-bottles-already-cover)).
- **A repo grant brings only its forge's description.** A grant resolves
  through the host's connection to its forge, so nothing is stacked for it
  (ADR 0019).
- **Only bottles whose secrets are here.** A proposal that needs a secret this
  host lacks is dropped or marked **can't run**
  ([details](#only-bottles-whose-secrets-are-here)).
- **Edits.** An edit is checked by loading the bottle as if accepted. Nothing
  is written until it's accepted ([details](#edits)).
- **Stacks are stacked as a launch stacks them.** For options 1–3 the
  additions are a bottle merged after `bottles` in order, exactly as
  `--bottle a --bottle b` merges them ([details](#stacking)).

#### Grants on known forges

A repo grant is kept on a forge this host connected. It is also kept on one
this host hasn't connected that is known without a connection
(`suggest/forges.py`). A forge is known that way when either:

- a bottle in the search path describes it (`git-gate.forges.<host>`), or
- the project's `origin` is on it.

For an `origin`, its kind is asked of the server, as `forge connect` does.
That isn't done for an origin on an IP address.

The proposal carries such a forge's description (`forges`, set by validation,
never by the model). So a saved bottle describes it and the review offers to
connect it.

A grant on a forge nothing describes becomes a note instead.

The additions bottle describes the forge of each grant (`git-gate.forges`). So
a saved bottle says what to connect elsewhere.

#### Nothing the bottles already cover

A dropped addition gets a note naming the bottle: "dropped 1 addition already
covered by basic: egress registry.npmjs.org".

This matters beyond tidiness. A later bottle's route replaces an earlier one
for the same host. So a `GET` addition would narrow `basic`'s `GET`/`HEAD`
route.

| Addition | Covered when |
|---|---|
| egress | the base route allows every method it asks for on every path, and it injects no credential |
| repo grant | the base has the same repo with enough access |
| env var | the base sets it |

A `stack+additions` proposal left with no additions becomes `stack` or
`reuse`.

#### Only bottles whose secrets are here

Each injected egress credential names a secret. So does each granted repo's
forge token. The launch resolves these from the host env or the secrets file.

A proposed bottle whose secret isn't set is dropped. Its note names the
secret, never its value. The stack falls back to `basic` when nothing left
starts from it.

Some proposals can't do without that bottle or secret:

- a new bottle extending that bottle
- a grant whose forge token is missing

Such a proposal is marked **can't run**, with the reason. Neither the guided
`start` nor `--auto` runs it.

A grant's token is its forge's default user's. The architect's library lists
each bottle's `missing_secrets`, so it can avoid proposing it.

#### Edits

- An edit never touches a built-in bottle. A host bottle that extends it is
  edited instead.
- It names a host bottle that exists, or a new name that's free.
- It is checked by loading the bottle as if it were accepted. One that doesn't
  load is dropped with a note.
- Its secret-looking env names are dropped with a note.
- Its grants on forges nothing describes are dropped with a note. A known
  forge this host hasn't connected is kept, as for additions.

The stack and its additions are then validated with every pending edit applied
(`suggest/edits.py`'s `overlay`). So the stack may name an edited bottle, and a
new one by its name.

Nothing is written until it's accepted. Agents and built-ins are never edited.

#### Stacking

The additions bottle has no `extends`. `--write NAME` saves the additions and
prints the `--bottle` flags to launch the stack with.

## Paths

`bottle-architect` answers first. When it can't, the rules path answers and
the proposal's notes say why:

- there's no agent file
- the host controller isn't reachable
- its bottle didn't boot
- the harness isn't logged in on this host
- it failed or timed out
- its answer wasn't a proposal

`--rules` skips the architect, and runs locally without the host controller.
Either way the proposal is validated against the bottles the project sees, and
`source` says which path produced it.

The suggestion runs in the host controller, for every caller:

- `bot-bottle bottles suggest` and `bot-bottle start` send the task there
  (`POST /suggest`).
- The console's launch page asks over the link.

So all three get the same flow and share the architect's warm bottle. A reply
sends the conversation with the task (`turns`: each earlier proposal, what was
done with it, and the reply).

Without a project directory, the project's bottle, origin and lockfiles aren't
considered, and the proposal says so. The console is one such caller when it
doesn't send one. The host's home is never used as the project.

### `bottle-architect` (default)

`bottle-architect` is the agent `setup` writes to
`~/.bot-bottle/agents/bottle-architect.md`. The suggester reads that file:

- its body is the system prompt
- `agent_provider.harness` picks the harness
- `model:` (optional) picks the model

Edit the file to change any of them. Only the host's agent is read, never a
project's.

It runs in its own bottle, `bottle-architect`, built in and never listed in
the library. The bottle holds its model API and nothing else:

- the key the gateway injects
- no repos
- no secrets
- no supervise tools
- an empty workspace

That bottle is what contains it, so any harness can be the architect. The call
inside it is the harness's ordinary headless mode (`claude -p`, codex, pi).

The agent file's body is its prompt file. The call adds `--model <model>` when
the file sets one, and runs under a 120-second timeout. A timed-out call is
stopped in the bottle too, not just on the host.

For claude the call is one-shot:

- no tools and no MCP servers. A call that explored its empty home with them
  took 28 s instead of 5 s.
- a one-line system prompt in place of Claude Code's own. The agent file's
  body is still appended.
- `--effort low`
- no saved session

**Always on.** Booting a bottle takes far longer than the model call. So the
host controller boots the architect's bottle when it starts and keeps it
running (`BOT_BOTTLE_ARCHITECT_ALWAYS_ON`, on by default).

It's deliberately tiny for that: 512 MiB and one CPU. 512 MiB is the bottle
floor, and claude peaks at about 320 MiB per call. One CPU answered as fast as
1 GiB did.

A bottle removed from under the host controller (by `bot-bottle cleanup bots`,
say) is replaced on the next suggestion. That suggestion then waits for the
boot.

With `BOT_BOTTLE_ARCHITECT_ALWAYS_ON=0` it boots on demand instead:

- `bot-bottle start` asks for it as soon as it starts (`POST /architect/warm`).
  So the boot overlaps choosing the harness and typing the task.
- Otherwise the first suggestion boots it.
- It then stays up for `BOT_BOTTLE_ARCHITECT_IDLE` seconds after its last
  suggestion. The default is 600, and `0` stops it after each suggestion.

The settings come with each request, so the CLI's setting takes effect on its
next call.

Each suggestion is a fresh headless call in it, never one long conversation,
so tasks don't bleed into each other. The prompt's static part (the agent's
prompt) comes first. Claude's and codex's automatic prompt caching reuse it
across calls.

| Host | Warm | Cold boot adds |
|---|---|---|
| macOS (macos-container, claude, its default model) | 3–5 s per suggestion end to end | about 25 s with cached images |
| Firecracker, measured before the one-shot call (approximate) | about 7–8 s | about 26 s (80 s when the images have to be built) |

The input is:

- the task
- the host's bottle library (names, about text, `extends`, compact elements,
  and the `missing_secrets` it needs that this host lacks)
- project metadata: origin URL, the project bottle's name, and
  lockfile/manifest *file names* (never their contents)
- the forges a grant may be on ([below](#forges-in-the-input))
- secret *names* from the host secrets file
- how to propose `edits` (built-in bottles are marked `builtin`), and to
  keep what only this task needs in `additions`
- after a reply, the conversation so far (`suggest/turns.py`)

The answer is parsed as a proposal and validated like any other.

#### Forges in the input

The input lists the forges this host connected, with host, kind and URL. Then it
lists the known ones it hasn't (`connected: false`).

It comes with two rules. A grant needs no bottle stacked for its forge. And
one on an unconnected forge is proposed as any other, because the person
connects it before the launch.

**Harnesses.** Any harness bot-bottle runs (claude, codex, pi, or a user
plugin) can be the architect. An unknown one is a note, and the rules answer.

If the agent file switches harness while the bottle is warm, that suggestion
falls back to the rules. The next one boots a bottle with the new harness.

### Rules (`--rules`, and the fallback)

The rules path never calls a model, and ignores the task. It stops at the
first that fits:

1. **The project bottle (`./main`) if trusted.** When the project directory
   has a trusted `.bot-bottle/bottles/main.md`, the proposal is
   `option: reuse` with `./main`.
2. **Origin + connected forge.** `git remote get-url origin` returns a URL
   whose host matches a forge this host connected (its web or SSH address).
   The proposal is `option: stack+additions` with `basic` and a `:write`
   grant on the origin repo.
3. **`basic`.** No trusted project bottle and no connected forge (or no
   project directory): `option: reuse` with `basic`.

## Details

The guided `start`'s screen, step by step.

### Screen layout

The guided `start` draws on the launch screen (`cli/screen.py`,
`commands/guided_view.py`). A step list sits beside the bottle.

Each finished step collapses to one line with a ✓. The launch's stages are
drawn under the steps on the same screen.

### Agent step

The agent you last ran is remembered in `~/.bot-bottle/start.json`. Without
one, the pick lists the logged-in harnesses that have an agent (`setup`'s
detection).

`bot-bottle start codex` runs that one and makes it the default. With no agent
set up, Enter runs `setup` on the screen, its lines under the step.

### Suggest step

The spinner says "warming the architect…" while its bottle still boots. The
architect's warm bottle started booting when `start` did. So usually only the
model call is left.

### The glance

The review first shows the proposal at a glance (`suggest/review.py`'s
`glance`):

```
Builds on dev. Adds Linear so the agent can read tickets
and label issues.

new  api.linear.app · acme/app (push) · $LINEAR_TOKEN
⚠    git.example.com isn't connected here · c connects it
```

1. The architect's gist: at most two short sentences, shaped "Builds on X.
   Adds Y so the agent can Z."
2. One `new` line listing what this run adds that no bottle it builds on
   already has. That is hosts, repos (with `(push)` when writable), env vars,
   and the secrets routes carry.
3. A `⚠` line for each forge a repo is on that this host hasn't connected yet.

The gist only describes. The `new` line is always built from the proposal.

A gist is replaced by a sentence made from the proposal (`Run in basic, adding
1 host.`) when it is missing, is over 160 characters, or names a host the
proposal doesn't reach.

### The details card

`d` shows the details:

```
new             api.linear.app     read tickets (GET, POST)
                                   with $LINEAR_TOKEN
                acme/app           label issues (push)
dev             14 hosts           (any)
                5 repos            acme/* ×5 (push)
basic           13 hosts           Package registries and pub… (GET)
forge           git.example.com    as acme-bot
```

| Line | Shows |
|---|---|
| `new` | a line for each thing it adds, with the architect's why |
| each bottle it builds on, nearest first | a line of counts, with that bottle's about text (its body's first line) |
| `forge`, for each forge its repos are on | whom this host pushes as there, or that it isn't connected here |

Each line ends with what it permits, in brackets:

- a host's HTTP methods. HEAD and OPTIONS go with GET. It shows `any` past
  three, or for a route that isn't inspected.
- a repo's `push` or `read`.

A secret a route carries follows (`with $LINEAR_TOKEN`).

A bottle's repos are counted and named in a few words. They are named by owner
when three owners or fewer hold them (`acme/* ×5`), else by two names and
`+N`.

Each element counts under the nearest bottle that declares it. Under the card
come every host, repo and env var under its bottle (↑/↓ scroll), then the
notes.

### Connecting a forge

`c` connects a forge on the screen:

1. It asks for the token, showing the page to make one at and the scopes it
   needs.
2. A spinner runs while it checks the token and pins the forge's SSH host key.
3. Once connected, the proposal is validated again, and `Enter` runs it.

When the SSH address the forge reports doesn't answer from here, it asks for
one this machine reaches (a VPN's, say). The one tried is filled in to edit.

A refused token asks again. `esc` backs out. GitHub's sign-in takes the plain
terminal.

A paste arrives as one piece (bracketed paste). So pasted text never presses
the review's keys.

### Editing the proposal

`e` opens the proposal as YAML in `$VISUAL`/`$EDITOR`. After you save, it's
validated.

It reopens with the errors inline until it validates. Emptying the file
cancels the edit.

### Edits to other bottles

A proposal can also change the setup, not only this run. It might add the
project's forge and repo to `./main`, say, so every later run has them.

```
edit ./main  + acme/api (push)
             the project's forge
✓    dev: edited (a new version)
```

Each edit is shown above the `new` line, the first pending one highlighted,
its why under it.

| Accepted edit to | Is written |
|---|---|
| a project bottle | in the repo (it shows in `git status`, for you to commit), and trusted on this host, so the launch doesn't ask again |
| a host bottle | as a new version (`bot-bottle bottles history`) |

The file keeps its comments and how its unchanged lines were written
(`contract/yaml_comments.py`). An allowed request kept in a bottle is written
the same way.

An edit whose repos are on a forge this host hasn't connected waits for `c`.

The review shows the stack as if every pending edit were accepted. After each
decision the proposal is validated again. So a stack that named a skipped new
bottle drops it.

### Replying

`r` takes a line and asks the architect again, with the conversation replayed
(`suggest/turns.py`):

- each earlier proposal
- what you did with it: edits accepted or skipped, forges connected
- your replies

Your replies stay above the gist, a line each. Each turn is still a fresh
headless call. The architect's bottle keeps no session.

### The console's review

The console's launch page shows the same review (its requests:
[host-controller.md](host-controller.md)). It has Accept/Skip on each edit,
its diff, and a reply box. A forge to connect shows the `forge connect`
command to run on the host.

### Prompts on the screen

The launch's questions are asked inline on the screen, under the steps:

- trust a project bottle
- confirm a git-gate host key
- type a secret
- restart an updated host controller

`setup`'s questions are asked the same way. Only `e`'s editor takes the normal
screen.

A command run on its own asks the terminal as before. In `lib/prompt.py`, a
view installs a prompter for its context, as `log.redirect` does for output.

## Hosted suggester

The rules path and `bottle-architect` on your own model login are what ships.
A hosted suggester is planned and not available yet.
