#!/bin/sh
# Egress daemon entrypoint inside the sidecar bundle (PRD 0024).
#
# Extracted verbatim from Dockerfile.egress's prior inline `sh -c`
# ENTRYPOINT so the supervisor in claude_bottle/sidecar_init.py can
# call it as a normal child. Behavior is unchanged:
#
#   * Upstream proxy: when EGRESS_UPSTREAM_PROXY is set, switch
#     to `--mode upstream:URL` to forward all post-MITM traffic
#     through pipelock. mitmproxy does NOT honor HTTPS_PROXY on
#     its outbound side, so the upstream wiring has to be the
#     mitmproxy mode flag, not env.
#   * Upstream trust: when EGRESS_UPSTREAM_CA is set, build a
#     combined trust bundle (system roots + pipelock CA) and point
#     mitmproxy at it. The option REPLACES mitmproxy's default
#     trust store, so passing pipelock's CA alone would break
#     pipelock-passthrough hosts (api.anthropic.com etc.).
#   * `-s /app/egress_addon.py` loads the addon that reads
#     /etc/egress/routes.yaml.

set -e

MODE="--mode regular@9099"
if [ -n "$EGRESS_UPSTREAM_PROXY" ]; then
  MODE="--mode upstream:$EGRESS_UPSTREAM_PROXY --listen-port 9099"
fi

TRUST_FLAG=""
if [ -n "$EGRESS_UPSTREAM_CA" ] && [ -f "$EGRESS_UPSTREAM_CA" ]; then
  COMBINED=/home/mitmproxy/.mitmproxy/combined-trust.pem
  cat /etc/ssl/certs/ca-certificates.crt "$EGRESS_UPSTREAM_CA" > "$COMBINED"
  TRUST_FLAG="--set ssl_verify_upstream_trusted_ca=$COMBINED"
fi

exec mitmdump $MODE $TRUST_FLAG -s /app/egress_addon.py
