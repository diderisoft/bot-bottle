"""Wire constants shared by the egress addon, git-http, supervise and the
git-gate renderer."""

# Identity token header for git-http and supervise. Egress gets the token as
# proxy credentials (`HTTPS_PROXY=http://<bottle_id>:<token>@gw`). Never
# forwarded upstream.
IDENTITY_HEADER = "x-bot-bottle-identity"

# Timeout for one git-gate step (the access hook, or git http-backend with its
# pre-receive hook): long enough for the first fetch of a large repo from
# upstream, a push's scan and forward, and a `# gitleaks:allow` approval (up to
# SUPERVISE_GITLEAKS_ALLOW_TIMEOUT_SECONDS, 300), short enough that a hung
# upstream can't hold a request forever.
GIT_GATE_TIMEOUT_SECS = 600

# git-gate's smart-HTTP port.
GIT_HTTP_PORT = 9420

# The orchestrator's signing key; held by it and the host CLI, never the data
# plane.
ORCHESTRATOR_TOKEN_ENV = "BOT_BOTTLE_ORCHESTRATOR_TOKEN"
# The data plane's `gateway` token, presented on `/resolve` and
# `/supervise/{propose,poll}`.
ORCHESTRATOR_AUTH_JWT_ENV = "BOT_BOTTLE_ORCHESTRATOR_AUTH_JWT"
