"""Names under which a bottle's secrets travel with its egress tokens.

At registration the host hands the orchestrator one map per bottle: the
upstream credentials the gateway injects (`EGRESS_TOKEN_*`), plus the secrets
the bottle itself holds (`HELD_*`): its exfil canary, and the values of its
`?` secret env entries. The gateway gets the map back on every `/resolve` and
scans against it:

- **egress** blocks the injected tokens anywhere in a request (under the
  route's on-match policy), and the canary always, as a hard block that can't
  be approved or redacted. It does not block the bottle's own secret env values, which an agent may
  legitimately send to the host they belong to (a `GH_TOKEN` to GitHub).
- **git-gate** refuses a push that carries any of them: nothing the bottle
  holds belongs in a commit.
"""

from __future__ import annotations

EGRESS_TOKEN_PREFIX = "EGRESS_TOKEN_"
HELD_PREFIX = "HELD_"
CANARY_KEY = "HELD_CANARY"
HELD_ENV_PREFIX = "HELD_ENV_"

# Keys whose values git-gate refuses in a push. (Egress scans the
# EGRESS_TOKEN_* values under the route's on-match policy, and the canary as
# an unconditional hard block.)
PUSH_SCAN_PREFIXES = (EGRESS_TOKEN_PREFIX, HELD_PREFIX)

# Shorter secret env values are not registered: an exact match on a short
# value (a PIN, a username) would refuse ordinary pushes.
MIN_HELD_SECRET_LEN = 8

__all__ = [
    "CANARY_KEY",
    "EGRESS_TOKEN_PREFIX",
    "HELD_ENV_PREFIX",
    "HELD_PREFIX",
    "MIN_HELD_SECRET_LEN",
    "PUSH_SCAN_PREFIXES",
]
