---
# The slugs project's bottle: what the demo's agent may reach beyond basic.
extends: basic
egress:
  routes:
    # One API the agent may call, POST included. What it sends is scanned; a
    # credential in it is refused on the spot (`block`, not held for
    # approval, so the demo doesn't wait on you).
    - host: httpbin.org
      methods: [GET, POST]
      dlp:
        on_match: block
git_gate:
  # The project's remote, a plain SSH server (the demo connects it and adds
  # its key on the host). Pushes go through the gate, whose gitleaks hook
  # refuses a secret before the upstream is ever dialled; the upstream
  # doesn't exist, so nothing leaves even if one got past.
  forges:
    upstream.invalid:
      kind: ssh
      host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDemoHostKeyNotARealOneJustAPlaceholder0
  repos:
    - upstream.invalid/demo/slugs:write
---

The slugs project: a tiny library, its tests, and one API.
