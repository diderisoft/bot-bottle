---
# The bottle `bottle-architect` runs in (PRD 0117): nothing but its model.
# No egress of its own (the agent's provider brings its model API route,
# with the key the gateway injects), no repos, no secrets, no supervise
# tools, and an empty workspace. Everything it decides from (the task, the
# bottle library, the project's metadata, forge and secret names) comes in
# through its prompt.
supervise: false
# Deliberately tiny, since it stays up: claude peaks at ~320 MiB per call, and
# 512 MiB with one CPU answered as fast as 1 GiB did. 512 MiB is the floor.
resources:
  memory: 512M
  cpus: 1
---

The bottle architect's own bottle. bot-bottle uses it to suggest bottles; it
isn't one to launch agents in.
