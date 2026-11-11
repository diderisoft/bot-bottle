# Projects

A project can ship bottles and agents in its repo's `.bot-bottle/` folder. They
run only once this host trusts their exact contents (`bot-bottle trust <name>`),
and any change asks again. Your own bottles and agents, in `~/.bot-bottle/`, run
as written.

Trust records live in the host database, `bot-bottle.db`
([architecture.md](architecture.md#databases)). The host controller writes
them, and the CLI asks it. `bot-bottle trust --list` shows them. The code is
[`manifest/trust.py`](../../bot_bottle/manifest/trust.py).

## Project bottles

A project bottle always needs trust.

- Project bottles are always named `./<name>`, so a project bottle never
  shares a name with one of yours. `bot-bottle <agent>` uses `./main` when the
  repo has one.
- The trust prompt lists every host and secret the bottle touches, including
  what it inherits through `extends`. So a cloned repo can't send a credential
  to another host without you seeing it first.
- Trust also records this host's connection to each forge the bottle's repos
  are on ([below](#what-forge-trust-records)).
- When one changes, the bottle asks to be trusted again, showing what changed
  (ADR 0019). Your own bottles don't.

### A project's first bottle

A terminal launch in a project with no project bottle first drafts one from the
checkout ([`onboarding.py`](../../bot_bottle/onboarding.py)). Once you accept
it, bot-bottle writes it as `./main` and trusts it, since bot-bottle wrote it.

The draft is built from the checkout, never by a model. It extends `basic` and
grants the project's `origin`, read-only, with the forge's description, when
this host connected that forge. When it hasn't, onboarding offers
`forge connect`.

### What forge trust records

For each forge, trust records its `kind`, `url`, `ssh` and host key. It also
records the user its grants push as, with that user's transport.

That user is the forge's default user, or the one the agent's
[`identity.accounts`](manifest.md#agents) picks. So an agent can't switch to
another of your accounts unseen.

## Project agents

A project agent needs trust when its `agent_provider` grants something:

- your login
- a model host with a secret
- an image build

It also needs trust when it runs on one of your bottles that grants more than
`basic`. A project agent with the same name as one of yours replaces it, and
the trust prompt says so.

### What an agent's trust covers

A project agent's trust hash covers three things:

- its `.md`
- what it gets from the host
- every file its image build takes from the launch directory

The build files are a custom `agent_provider.dockerfile_path`'s bytes, and an
overlay's staged build context. That context is its Dockerfile plus that
Dockerfile's whole directory, or its `files`.

[`agent_overlay.inputs_digest`](../../bot_bottle/agent_overlay.py) digests the
overlay context. It is the overlay cache key's context, staged on a placeholder
base, so the two can't drift.

Editing any of these files asks again. So does a missing input appearing.

Trust records from before builds were covered have no `scheme`. They re-prompt
once for agents that build, and the prompt says why.
