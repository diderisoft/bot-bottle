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
  --architect      use bottle-architect instead of the rules path (see below)
  --json           print raw proposal JSON to stdout
```

### Example

```
$ bot-bottle bottles suggest "deploy bot-bottle-console to Fly staging"
option:  reuse
bottles: basic
note:    rules path: no project bottle or connected forge found; using basic

elements preview:
  egress   *.docker.io               any request  [basic]
  egress   *.ghcr.io                 any request  [basic]
  ...
```

With a connected Gitea forge and a project `origin`:

```
$ bot-bottle bottles suggest "push a release"
option:  stack+additions
bottles: gitea.dideric.is, basic
  + repo   gitea.dideric.is/didericis/bot-bottle:write  # write access to the project's own repo
note:    rules path: origin didericis/bot-bottle on gitea.dideric.is
```

## The proposal

A proposal is a JSON object with these fields:

| Field | Type | Description |
|---|---|---|
| `option` | string | `reuse`, `stack`, `stack+additions`, `new-extending`, or `new` (preference order) |
| `bottles` | array | existing bottles to use or extend |
| `new_bottle` | object or null | for `new-extending`/`new`: `{name, extends, about}` |
| `additions.egress` | array | routes to add: `{host, methods, why}` |
| `additions.repos` | array | repo grants: `{grant, why}`, e.g. `gitea.example.com/owner/repo:write` |
| `additions.env` | array | env var names: `{name, why}` — values come from the secrets file |
| `additions.credentials` | array | injected secrets: `{name, route, why}` — a secret name and the egress host it goes on |
| `similar` | array | existing bottles close to the proposal: `{bottle, covers}` |
| `notes` | array | free-text caveats |

### Constraints

- **`inspect: false` is never proposed.** Routes always allow TLS inspection.
  A user who needs pass-through edits the bottle.
- **Secret values are never included.** `additions.credentials` carries only a
  secret *name* from the host secrets file; the value is injected at launch by
  the gateway.
- **Grants only on connected forges.** A repo on a forge no bottle declares
  becomes a note instead of a grant.
- **Validation.** The proposal is turned into a bottle document and loaded with
  the real manifest loader.  Any element that fails is dropped and noted rather
  than failing the whole proposal.

## Paths

### Rules (default)

The rules path never calls a model.  It stops at the first that fits:

1. **The project bottle (`./main`) if trusted.** When the current directory has
   a trusted `.bot-bottle/bottles/main.md`, the proposal is `option: reuse` with
   `./main`.
2. **Origin + connected forge.** When `git remote get-url origin` returns a URL
   whose host matches a forge any host bottle declares, the proposal is
   `option: stack+additions` with the forge bottle, `basic`, and a `:write` grant
   on the origin repo.
3. **`basic`.** No project bottle, no connected forge: `option: reuse` with
   `basic`.

### `bottle-architect` (`--architect`)

`bottle-architect` is an agent manifest `setup` writes to
`~/.bot-bottle/agents/bottle-architect.md`.  When `--architect` is passed,
`suggest` runs it in print mode on the host (no sandbox, no tools) with:

- The task
- The host's bottle library (names, about text, `extends`, compact elements)
- Project metadata: origin URL, the project bottle's name, and
  lockfile/manifest *file names* (never their contents)
- Connected forges (names and URLs)
- Secret *names* from the host secrets file

The agent outputs a JSON object matching the proposal schema.  Invalid JSON or
a schema error falls back to the rules path automatically.

The agent uses `forward_host_credentials: true`, so it uses whichever harness
(`claude` or `codex`) is logged in on the host.  If neither is logged in, it
falls back to the rules path.  Edit
`~/.bot-bottle/agents/bottle-architect.md` to change the model, prompt, or
provider.

## `--write NAME`

`--write NAME` saves the validated proposal as a new host bottle at
`~/.bot-bottle/bottles/<NAME>.md`.  The bottle is a regular manifest file
that can be inspected, edited and extended like any other.

The bottle's `extends:` follows from the proposal:

- `reuse` with one bottle → `extends: <bottle>`
- `stack` with two or more → `extends: [<bottle1>, <bottle2>]`
- `stack+additions` → same extends, plus egress/repos/env from additions
- `new-extending` → `extends: <new_bottle.extends>`, plus additions
- `new` → no extends (or `extends: basic`), plus additions

## Free/paid split

| Free (open core) | Paid (hosted) |
|---|---|
| rules path; `bottle-architect` on your own model login | hosted suggester on the console launch page (fast, Anthropic's model, no setup) |

The hosted path is built in a later chunk (PRD 0117, design repo).  A failure
in the hosted path always falls back to the rules path, so a launch never waits
on the model.
