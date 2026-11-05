---
# The bottle `bottle-architect` runs in (PRD 0117): nothing but its model.
# No egress of its own (the agent's provider brings its model API route,
# with the key the gateway injects), no repos, no secrets, no supervise
# tools, and an empty workspace. Everything it decides from (the task, the
# bottle library, the project's metadata, forge and secret names) comes in
# through its prompt.
supervise: false
resources:
  memory: 1G
---

The bottle architect's own bottle. bot-bottle uses it to suggest bottles; it
isn't one to launch agents in.
