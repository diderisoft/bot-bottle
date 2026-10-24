"""Names under which a bottle's secrets travel with its egress tokens.

Registration gives the orchestrator one map per bottle: injected upstream
credentials (`EGRESS_TOKEN_*`) and the secrets the bottle holds (`HELD_*`: its
canary and its `?` env values). The gateway gets it back on `/resolve`:

- egress blocks the injected tokens per the route's on-match policy, and the
  canary always. It doesn't block the bottle's own env secrets, which may
  legitimately go to their own host.
- git-gate refuses a push carrying any of them.
"""

from __future__ import annotations

EGRESS_TOKEN_PREFIX = "EGRESS_TOKEN_"
HELD_PREFIX = "HELD_"
CANARY_KEY = "HELD_CANARY"
HELD_ENV_PREFIX = "HELD_ENV_"

# Keys whose values git-gate refuses in a push.
PUSH_SCAN_PREFIXES = (EGRESS_TOKEN_PREFIX, HELD_PREFIX)

# Shorter env secrets aren't registered; exact matches on short values would
# refuse ordinary pushes.
MIN_HELD_SECRET_LEN = 8

__all__ = [
    "CANARY_KEY",
    "EGRESS_TOKEN_PREFIX",
    "HELD_ENV_PREFIX",
    "HELD_PREFIX",
    "MIN_HELD_SECRET_LEN",
    "PUSH_SCAN_PREFIXES",
]
