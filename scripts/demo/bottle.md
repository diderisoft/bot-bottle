---
# Demo bottle — the boundary the recorded GIF exercises. Installed to
# $HOME/.bot-bottle/bottles/demo.md by scripts/demo-setup.sh and removed
# again by scripts/demo-teardown.sh. Bottles may only live under $HOME
# (manifest/index.py refuses a bottles/ dir in CWD — the filesystem
# layout is the trust boundary, PRD 0011), so setup writes into real
# config and teardown must restore it.
#
# The Claude provider is the demo agent's (scripts/demo/agent.md), so
# this bottle carries only the boundary.

env:
  # Synthetic GitHub-PAT-shaped value. Never a real credential — it
  # exists so probe 3 has something for the egress scanner's
  # token_patterns detector to catch.
  FAKE_TOKEN: ghp_aB3cD4eF5gH6iJ7kL8mN9oP0qR1sT2uV3wX4yZ

egress:
  routes:
    # The single non-provider allowlist entry. example.com is
    # deliberately absent so probe 2 gets a hard 403 from the host
    # filter, while this host passes the filter and leaves probe 3 to
    # be decided by the DLP body scan alone.
    #
    # dlp.on_match: block is load-bearing for the recording. The
    # default is `supervise`, which holds the request open awaiting an
    # operator decision in `bot-bottle supervise` for up to
    # EGRESS_TOKEN_ALLOW_TIMEOUT_SECONDS (300s) — that would stall the
    # tape. `block` reproduces the immediate 403 the demo is showing off.
    - host: example.org
      dlp: { on_match: block }

git_gate:
  user:
    name: demo
    email: demo@example.invalid
  repos:
    demo-upstream:
      # Unreachable on purpose. gitleaks runs in the gate's pre-receive
      # hook and rejects the ref before the gate would ever dial the
      # upstream, so probe 4 never depends on the network.
      url: ssh://git@upstream.invalid/path.git
      key:
        provider: static
        path: ~/.cache/bot-bottle-demo/fake-key
      host_key: ssh-ed25519 AAAAEXAMPLE
---

The `demo` bottle — the sandbox boundary behind `docs/demo.gif`.

Declares exactly enough to make all four recorded probes meaningful:
one allowlisted host, one synthetic credential in the environment, and
one git upstream wired through the git-gate. Everything an agent could
use to reach the network here is either denied by the host filter,
caught by the egress DLP scan, or rejected by gitleaks at push time.

Not an example to copy for real work — the fake token and the
`upstream.invalid` remote only make sense for a scripted recording.
See `examples/bottles/` for bottles meant to be adapted.
