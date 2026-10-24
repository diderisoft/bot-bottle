#!/bin/sh
# Egress entrypoint, started by the gateway supervisor.
#
# - EGRESS_UPSTREAM_PROXY chains through an upstream proxy with `--mode
#   upstream:URL` (mitmproxy ignores HTTPS_PROXY outbound).
# - EGRESS_UPSTREAM_CA adds a CA to the system roots for upstream trust (the
#   option replaces mitmproxy's default store).
# - The addon resolves each request's policy from the orchestrator.

set -e

# Fail closed without a policy source, independent of how mitmproxy handles
# the addon's own load error.
if [ -z "$BOT_BOTTLE_ORCHESTRATOR_URL" ]; then
  echo "egress: BOT_BOTTLE_ORCHESTRATOR_URL is required (no static routes fallback)" >&2
  exit 1
fi

# Pin the confdir to where the CA is mounted; under root `~/.mitmproxy` would
# mint a fresh, untrusted CA.
CONFDIR=/home/mitmproxy/.mitmproxy
CONFDIR_FLAG="--set confdir=$CONFDIR"

MODE="--mode regular@9099"
if [ -n "$EGRESS_UPSTREAM_PROXY" ]; then
  MODE="--mode upstream:$EGRESS_UPSTREAM_PROXY --listen-port 9099"
fi

# EGRESS_LISTEN_HOST overrides the bind address.
LISTEN_HOST_FLAG=""
if [ -n "$EGRESS_LISTEN_HOST" ]; then
  LISTEN_HOST_FLAG="--listen-host $EGRESS_LISTEN_HOST"
fi

TRUST_FLAG=""
if [ -n "$EGRESS_UPSTREAM_CA" ] && [ -f "$EGRESS_UPSTREAM_CA" ]; then
  COMBINED=$CONFDIR/combined-trust.pem
  cat /etc/ssl/certs/ca-certificates.crt "$EGRESS_UPSTREAM_CA" > "$COMBINED"
  TRUST_FLAG="--set ssl_verify_upstream_trusted_ca=$COMBINED"
fi

# Proxy env for mitmdump only, so other gateway daemons don't route through the
# upstream proxy.
if [ -n "$EGRESS_UPSTREAM_PROXY" ]; then
  export HTTPS_PROXY="$EGRESS_UPSTREAM_PROXY"
  export HTTP_PROXY="$EGRESS_UPSTREAM_PROXY"
  export NO_PROXY="localhost,127.0.0.1"
fi

exec mitmdump $CONFDIR_FLAG $MODE $LISTEN_HOST_FLAG $TRUST_FLAG -s /app/egress_addon.py
