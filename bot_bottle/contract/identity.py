"""Shared wire-protocol constants for gateway-bundled modules.

Single source of truth for values that appear across the egress addon,
git-http backend, supervise server, and git-gate renderer. Importing
from this module instead of duplicating the literals means a rename is
a one-line change and is caught by the type checker at the import site."""

# App-layer identity token header. Delivered as proxy credentials
# (HTTPS_PROXY=http://<bottle_id>:<token>@gw) by launch; the egress
# addon reads and strips it, the supervise server and git-http backend
# read it for attribution, and none of them forward it upstream.
IDENTITY_HEADER = "x-bot-bottle-identity"

# Shared timeout (seconds) for all git-gate subprocess and CGI calls:
# git daemon (--timeout/--init-timeout), the access-hook subprocess in
# git_http_backend, and the git http-backend CGI subprocess.
GIT_GATE_TIMEOUT_SECS = 15

# The gateway's git-gate smart-HTTP port, which agents push and fetch through.
GIT_HTTP_PORT = 9420

# The orchestrator's control-plane signing key. Held only by the orchestrator
# (to verify tokens) and the host CLI (to mint its own); never by the data plane.
ORCHESTRATOR_TOKEN_ENV = "BOT_BOTTLE_ORCHESTRATOR_TOKEN"
# The data plane's pre-minted `gateway`-role token. The gateway presents it on
# /resolve and /supervise/{propose,poll}; it never holds the signing key.
ORCHESTRATOR_AUTH_JWT_ENV = "BOT_BOTTLE_ORCHESTRATOR_AUTH_JWT"
