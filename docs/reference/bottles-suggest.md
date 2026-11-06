# `bottles suggest`

`bot-bottle bottles suggest "<task>"` proposes the bottle a task needs before
it runs (PRD 0117, design repo).  The output is always a typed proposal — never
free YAML — so the manifest loader can validate it and the pretty view can show
what it contains.

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
same flow (`suggest.flow`): `bottle-architect` first, the rules path when it
can't answer.

## The guided `bot-bottle start`

On a terminal, `bot-bottle start` without `--bottle` is guided. It draws on
the launch screen: a step list beside the bottle, each finished step
collapsing to one line with a ✓, and the launch's stages drawn under the
steps on the same screen (`cli/screen.py`, `commands/guided_view.py`):

1. **Agent.** The agent you last ran (remembered in
   `~/.bot-bottle/start.json`), else a pick among the logged-in harnesses
   that have an agent (`setup`'s detection). `bot-bottle start codex` runs
   that one and makes it the default. With no agent set up, Enter runs
   `setup` and comes back.
2. **Task.** What the agent should do. Empty runs the agent in `basic`, as a
   plain start, with no suggestion.
3. **Suggest**, for the current project, with a spinner ("warming the
   architect…" while its bottle still boots). The architect's warm bottle
   started booting when `start` did, so usually only the model call is left.
4. **Review.** The proposal in brief: one plain sentence on what it
   recommends (the architect's `summary`, else one made from the proposal:
   `Run in basic + gitea, adding 1 repo.`), what the bottle allows counted
   (`allows 16 hosts · 1 repo`), what it adds counted, the bottles it reuses,
   and how many notes it has. `Enter` runs it, `s` saves it as a named
   bottle first, `e` edits it, `d` shows what it adds (with why), its notes
   and every element, `q` cancels. Edit opens the proposal as YAML in
   `$VISUAL`/`$EDITOR`; after you save, it's validated, and it reopens with
   the errors inline until it validates (emptying the file cancels the
   edit). A proposal that can't run on this host (below) offers only edit
   and cancel.
5. **Launch** in that bottle with the task as the agent's first prompt.

`--auto`, `--verbose` and `--dry-run` keep plain output, and nothing draws
without a terminal. Ctrl-C quits without launching. Bare `bot-bottle`, on a
terminal, is a home menu that starts this same flow (`commands/home.py`).

**Nothing is saved unless you ask** (ADR 0017). A suggestion that adds
anything runs as the run's own bottle, `suggested`: kept with the run (a
resume finds it, the console shows it read-only, the audit has its full
text), never added to the library. "Save it as a bottle" writes it like
`--write`, versioned like any host bottle, and runs that.

**`--auto`** (ADR 0018) skips step 4: it prints one line saying what it chose
(`--auto: running in basic, adding egress api.open-meteo.com (from
architect)`) and runs it, unsaved. Anything the bottle missed goes through
supervise approvals at runtime. `BOT_BOTTLE_START_AUTO=1` makes it the
default; `--no-auto` asks again for one run.

**Without a terminal nothing prompts.** `--auto` is then the only way to use
a suggestion, and the task comes as an argument: `bot-bottle start --auto
"fix the flaky test"` (an agent can go first: `bot-bottle start codex --auto
"…"`). Without `--auto`, give `--bottle` (or `--headless`).

`bot-bottle start claude --bottle web` launches exactly as before.

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

With a connected Gitea forge and a project `origin`:

```
$ bot-bottle bottles suggest --rules "push a release"
Run in gitea.example.com + basic, adding 1 repo.

option:  stack+additions
from:    rules
bottles: basic
  + repo   gitea.example.com/acme/app:write  # write access to the project's own repo
note:    rules path: origin acme/app on gitea.example.com
```

## The proposal

A proposal is a JSON object with these fields:

| Field | Type | Description |
|---|---|---|
| `summary` | string | one plain sentence on what it recommends and why, for the person deciding; asked for in the request, so architect prompts written before it still give one. Cleared when validation or the runnability check drops part of the proposal, so the review makes one from what's left |
| `option` | string | `reuse`, `stack`, `stack+additions`, `new-extending`, or `new` (preference order) |
| `bottles` | array | existing bottles to use or extend |
| `new_bottle` | object or null | for `new-extending`/`new`: `{name, extends, about}` |
| `additions.egress` | array | routes to add: `{host, methods, why}` |
| `additions.repos` | array | repo grants: `{grant, why}`, e.g. `gitea.example.com/owner/repo:write` |
| `additions.env` | array | env var names: `{name, why}` — values come from the secrets file |
| `additions.credentials` | array | injected secrets: `{name, route, why}` — a secret name and the egress host it goes on |
| `similar` | array | existing bottles close to the proposal: `{bottle, covers}` |
| `notes` | array | free-text caveats, including why the rules answered when the architect couldn't |
| `source` | string | which path produced it: `architect` or `rules` (set by the flow, never by the model) |

### Constraints

- **`inspect: false` is never proposed.** Routes always allow TLS inspection.
  A user who needs pass-through edits the bottle.
- **Secret values are never included.** `additions.credentials` carries only a
  secret *name* from the host secrets file; the value is injected at launch by
  the gateway.
- **No secrets in env.** An env var's value is visible to the agent, so an env
  name that looks like a secret (`TOKEN`, `SECRET`, `PASSWORD`, `API_KEY`, …)
  is dropped with a note to add it as a credential instead.
- **Grants only on connected forges.** A repo on a forge this host hasn't
  connected becomes a note instead of a grant.
- **Validation.** The proposal is turned into a bottle document and loaded with
  the real manifest loader.  Any element that fails is dropped and noted rather
  than failing the whole proposal.
- **Nothing the bottles already cover.** An addition the proposal's own bottles
  already allow is dropped, with a note naming the bottle ("dropped 1 addition
  already covered by basic: egress registry.npmjs.org"). This matters beyond
  tidiness: a later bottle's route replaces an earlier one for the same host,
  so a `GET` addition would narrow `basic`'s `GET`/`HEAD` route. An egress
  addition is covered when the base route allows every method it asks for on
  every path and it injects no credential; a repo grant when the base has the
  same repo with enough access; an env var when the base sets it. A
  `stack+additions` proposal left with no additions becomes `stack` or `reuse`.
- **A repo grant brings only its forge's description.** A grant resolves
  through the host's connection to its forge, so nothing is stacked for it
  (ADR 0019); the additions bottle describes the forge (`git-gate.forges`)
  so a saved bottle says what to connect elsewhere.
- **Only bottles whose secrets are here.** Each injected egress credential and
  each granted repo's forge token names a secret the launch resolves from the
  host env or the secrets file. A proposed bottle whose secret isn't set is
  dropped, with a note naming the secret, never its value (the stack falls
  back to `basic` when nothing left starts from it). When the proposal can't
  do without it (a new bottle extending that bottle, or a grant whose forge
  token is missing), it's marked **can't run**, with the reason, and neither
  the guided `start` nor `--auto` runs it. A grant's token is its forge's
  default user's. The architect's library lists each bottle's
  `missing_secrets`, so it can avoid proposing it.
- **Stacks are stacked as a launch stacks them.** For options 1–3 the additions
  are a bottle merged after `bottles` in order, exactly as
  `--bottle a --bottle b` merges them. It has no `extends`.
  `--write NAME` saves the additions and prints the `--bottle` flags to
  launch the stack with.

## Paths

`bottle-architect` answers first. When it can't, the rules path answers and
the proposal's notes say why: there's no agent file, the host controller isn't
reachable, its bottle didn't boot, the harness isn't logged in on this host,
it failed or timed out, or its answer wasn't a proposal. `--rules` skips the
architect, and runs locally without the host controller. Either way the
proposal is validated against the bottles the project sees, and `source` says
which path produced it.

The suggestion runs in the host controller, for every caller: `bot-bottle
bottles suggest` and `bot-bottle start` send the task there (`POST /suggest`),
and the console's launch page asks over the link. So all three get the same
flow and share the architect's warm bottle.

Without a project directory (the console when it doesn't send one), the
project's bottle, origin and lockfiles aren't considered, and the proposal
says so. The host's home is never used as the project.

### `bottle-architect` (default)

`bottle-architect` is the agent `setup` writes to
`~/.bot-bottle/agents/bottle-architect.md`, and the suggester reads that file:

- its body is the system prompt;
- `agent_provider.harness` picks the harness;
- `model:` (optional) picks the model.

Edit the file to change any of them. Only the host's agent is read, never a
project's.

It runs in its own bottle, `bottle-architect` (built in, never listed in
the library): its model API and nothing else, with the key the gateway
injects, no repos, no secrets, no supervise tools and an empty workspace.
That bottle is what contains it, so any harness can be the architect: the
call inside it is the harness's ordinary headless mode (`claude -p`, codex,
pi), with the agent file's body as its prompt file and `--model <model>` when
the file sets one, under a 120-second timeout. A timed-out call is stopped in
the bottle too, not just on the host. For claude the call is one-shot: no
tools and no MCP servers (a call that explored its empty home with them took
28 s instead of 5 s), a one-line system prompt in place of Claude Code's own
(the agent file's body is still appended), `--effort low` and no saved
session.

**Always on.** Booting a bottle takes far longer than the model call, so
the host controller boots the architect's bottle when it starts and keeps it
running (`BOT_BOTTLE_ARCHITECT_ALWAYS_ON`, on by default). It's deliberately
tiny for that: 512 MiB (the bottle floor; claude peaks at about 320 MiB per
call) and one CPU, which answered as fast as 1 GiB did. A bottle removed from
under the host controller (by `bot-bottle bots cleanup`, say) is replaced on
the next suggestion, which then waits for the boot.

With `BOT_BOTTLE_ARCHITECT_ALWAYS_ON=0` it boots on demand instead:
`bot-bottle start` asks for it as soon as it starts (`POST /architect/warm`),
so the boot overlaps choosing the harness and typing the task, and the first
suggestion otherwise boots it. It then stays up for
`BOT_BOTTLE_ARCHITECT_IDLE` seconds (default 600; `0` stops it after each
suggestion) after its last one. The settings come with each request, so the
CLI's setting takes effect on its next call.

Each suggestion is a fresh headless call in it, never one long conversation,
so tasks don't bleed into each other. The prompt's static part (the agent's
prompt) comes first, which claude's and codex's automatic prompt caching
reuse across calls.

Measured on macOS (macos-container, claude, its default model): 3–5 s per
suggestion end to end in the warm bottle; a cold boot first adds about 25 s
with cached images. On a Firecracker host before the one-shot call: 7–8 s
warm, 26 s more cold (80 s when the images have to be built).

The input is:

- the task;
- the host's bottle library (names, about text, `extends`, compact elements,
  and the `missing_secrets` it needs that this host lacks);
- project metadata: origin URL, the project bottle's name, and
  lockfile/manifest *file names* (never their contents);
- the forges this host connected (host, kind and URL), with the rule that a
  grant needs no bottle stacked for its forge;
- secret *names* from the host secrets file.

The answer is parsed as a proposal and validated like any other.

**Harnesses.** Any harness bot-bottle runs (claude, codex, pi, or a user
plugin) can be the architect; an unknown one is a note and the rules answer.
If the agent file switches harness while the bottle is warm, that suggestion
falls back to the rules and the next one boots a bottle with the new harness.

### Rules (`--rules`, and the fallback)

The rules path never calls a model, and ignores the task. It stops at the
first that fits:

1. **The project bottle (`./main`) if trusted.** When the project directory
   has a trusted `.bot-bottle/bottles/main.md`, the proposal is
   `option: reuse` with `./main`.
2. **Origin + connected forge.** When `git remote get-url origin` returns a URL
   whose host matches a forge this host connected (its web or SSH address),
   the proposal is `option: stack+additions` with `basic` and a `:write`
   grant on the origin repo.
3. **`basic`.** No trusted project bottle and no connected forge (or no
   project directory): `option: reuse` with `basic`.

## `--write NAME`

`--write NAME` saves the validated proposal as a new host bottle at
`~/.bot-bottle/bottles/<NAME>.md`, as a new version in the bottles history. It
refuses a name that isn't a bottle name or a bottle that already exists. The
console's "Use this bottle" writes the same way.

What it writes depends on the option:

- **`reuse`, `stack` and `stack+additions`:** only the additions (egress
  routes, repo grants, env names), with **no `extends`** unless it has repo
  grants, in which case it extends the proposal's bottles. The new bottle is
  stacked after the proposal's bottles, and the CLI prints the flags to launch
  it, e.g. `--bottle basic --bottle <NAME>`. A `reuse` or `stack` with no
  additions needs no new bottle: launch with the bottles it names.
- **`new-extending` and `new`:** the additions plus `extends:` from
  `new_bottle.extends`, else the first proposed bottle, else `basic`. Launch it
  on its own: `--bottle <NAME>`.

An env name is written with a `?NAME` placeholder, prompted for at launch. The
body is the new bottle's about text and the proposal's notes.

## Free/paid split

| Free (open core) | Paid (hosted) |
|---|---|
| rules path; `bottle-architect` on your own model login | hosted suggester on the console launch page (fast, Anthropic's model, no setup) |

The hosted path is built in a later chunk (PRD 0117, design repo).  A failure
in the hosted path always falls back to the rules path, so a launch never waits
on the model.
