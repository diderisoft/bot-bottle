# Security policy

bot-bottle is a sandbox for coding agents, so a way out of the sandbox is the
bug we most want to hear about. Please report it privately first.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: on
[github.com/diderisoft/bot-bottle](https://github.com/diderisoft/bot-bottle)'s
**Security** tab, choose **Report a vulnerability**
([direct link](https://github.com/diderisoft/bot-bottle/security/advisories/new)). Don't open a public issue or pull
request for a suspected vulnerability.

Include what you can of:

- the backend (macos-container, firecracker or docker) and host OS;
- the bottle manifest, or the smallest one that reproduces it;
- what the agent did and what it reached;
- the bot-bottle version or commit.

## What's in scope

- An agent reaching the network, the host, or another bottle other than
  through the gateway.
- A secret leaving a bottle through egress or git-gate when the bottle's
  policy should have stopped it, including a token the agent was never meant
  to see.
- An agent reaching or changing the control plane: the orchestrator, the host
  controller, the gateway's configuration, or another bottle's policy.
- A repository-shipped bottle or agent getting grants without the per-host
  trust prompt.
- The launch-time isolation probe passing when the agent isn't isolated.
- A paired console doing more than the threat model says pairing allows,
  such as reading a secret's value or decrypting audit records without a
  passkey-signed unlock.

## What's not a vulnerability

- Limitations the docs already state, such as the Docker backend sharing the
  host kernel, or what an allowlisted host can do with the requests a bottle
  is allowed to send it. The [threat model](docs/security/threat-model.md)
  lists what bot-bottle defends against and what it doesn't; a way past a
  defence it claims is in scope.
- An agent misbehaving inside its own bottle without crossing a boundary.
- Bugs in the model harnesses (Claude Code, Codex, pi) themselves; report
  those upstream.

## What to expect

bot-bottle has a single maintainer. Expect an acknowledgement within 7 days
and an initial assessment within 14. Fixes for confirmed vulnerabilities take
priority over other work. Once a fix ships, the report is disclosed with
credit unless you ask otherwise.
