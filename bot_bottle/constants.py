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
