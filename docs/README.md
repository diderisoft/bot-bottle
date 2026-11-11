# Welcome to bot-bottle

To get started, see [Getting started](start.md). Here's a brief overview of
the project's key ideas. Every term is defined in the
[glossary](reference/glossary.md).

## Bottles

A bottle is a safe, tightly controlled, reusable environment for running a
coding agent, defined in a single small Markdown file. Here's a simple bottle,
`app`, that allows an agent to push to `acme/app` and call one API:

```yaml
git_gate:
  repos: [github.com/acme/app:write]
egress:
  routes:
    - host: api.example.com
      auth: {type: bearer, value: secret:EXAMPLE_API_TOKEN}
```

Bottles define the boundary of an agent VM, and nothing can go in or out of the
bottle without explicit permission. Anything the bottle doesn't allow is
blocked, or comes to you to approve.

What does leave is checked for secrets. Outbound requests are scanned for
token-shaped strings and the bottle's own credentials, unless a route opts out
with `passthrough: true`. Every push is scanned too, and carries a
`Bot-Bottle:` trailer naming the bottle that made it.

Bottles stack, so each one can grant a single piece of access, like one
registry, repo or API.

For example, if you want to create a set of read-only permissions for
interacting with npm, you could define an `npm_reader` bottle like so:

```yaml
egress:
  routes:
    - host: registry.npmjs.org   # GET and HEAD only, so downloads but no publishing
```

Then launch with both: `bot-bottle start --bottle npm_reader --bottle app`. A
bottle can also build on others with `extends: npm_reader`. When two set the
same thing, the later one wins.

You don't have to write a bottle from scratch: `bot-bottle start` proposes one
for your task.

Reference: [bottles](reference/manifest.md#bottles),
[composing bottles](reference/manifest.md#composing-bottles),
[secret scanning](reference/gateway.md#outbound-dlp),
[push scanning](reference/git-gate.md).

## Agents

An agent is what runs inside a bottle: a VM (a container on Docker) with an
agent harness (Claude Code, Codex or pi), its skills and prompt, and optionally
an overlay on the built-in image. It has no access to the host, and holds only
the secrets its bottle puts in its env. Here's an agent, `app-helper`:

```yaml
agent_provider:
  harness: claude
  login: host            # the host's login, added on the way out
skills:
  - code-review
```

An agent names no bottle. You pick its bottles at launch:
`bot-bottle start app-helper --bottle npm_reader --bottle app`.

Reference: [agents](reference/manifest.md#agents),
[images and overlays](reference/manifest.md#images).

## Secrets

A secret is a value kept on the host: in the secrets file, your shell or your
harness's login. Bottles name secrets, like `secret:EXAMPLE_API_TOKEN` above, but never
hold them. Route credentials and logins are added to requests on the way out,
so the agent only sees a placeholder. A secret in a bottle's `env:` is the
exception: it reaches the agent as its real value.

Reference: [secrets](reference/secrets.md).

## Projects

A project can ship bottles and agents in its `.bot-bottle/` folder, so everyone
working on it shares them. A project bottle is named `./<name>`, so it never
clashes with one of yours.

A project bottle runs only after your host trusts its exact contents, and any
change to it asks again. So does a project agent that asks for more than the
basics, like your login or an image build.

Reference: [projects](reference/projects.md).

## Host

The host is the machine you run bot-bottle on, like your laptop or a server.
It's the trusted side of the boundary, and agents can't reach it. It keeps:

- your own bottles and agents, in `~/.bot-bottle/`
- your secrets
- which project bottles and agents you've trusted
- an audit journal of what each agent did

One host runs many agents at once, each in its own bottle.

Reference: [install](reference/install.md), [backends](reference/backends.md).

## Console

The console is an optional web app for watching agents and answering their
requests from your phone. One account can pair many hosts, so you can manage
all of them from one place.

Reference: [remote access](reference/host-controller.md#remote-access).

## Where to next

- [Getting started](start.md): install, your first agent and your first bottle
- [Reference](reference/README.md): how each part works now
- [Threat model](security/threat-model.md): what bot-bottle defends against,
  and what it doesn't
- [Escape attempts](security/escape-attempts.md): escape and exfiltration
  techniques tried against bot-bottle, each with the test that catches it
- [Decision records](decisions/README.md): why it's built this way
- [About these docs](about-these-docs.md): which document to write, and how

Found a way out? Report it privately through
[GitHub's vulnerability reporting](https://github.com/diderisoft/bot-bottle/security/advisories/new).
What's in scope is in [SECURITY.md](../SECURITY.md).
