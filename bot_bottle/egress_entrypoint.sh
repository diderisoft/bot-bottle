#!/bin/sh
# Egress daemon entrypoint inside the sidecar bundle (PRD 0024).
#
# Extracted verbatim from Dockerfile.egress's prior inline `sh -c`
# ENTRYPOINT so the supervisor in bot_bottle/sidecar_init.py can
# call it as a normal child. Behavior is unchanged:
#
#   * Upstream proxy: when EGRESS_UPSTREAM_PROXY is set, switch
#     to `--mode upstream:URL` to chain through an upstream proxy.
#     mitmproxy does NOT honor HTTPS_PROXY on its outbound side,
#     so the upstream wiring has to be the mitmproxy mode flag,
#     not env.
#   * Upstream trust: when EGRESS_UPSTREAM_CA is set, build a
#     combined trust bundle (system roots + upstream CA) and point
#     mitmproxy at it. The option REPLACES mitmproxy's default
#     trust store, so passing the upstream CA alone would break
#     non-chained hosts.
#   * `-s /app/egress_addon.py` loads the addon that reads
#     /etc/egress/routes.yaml.

set -e

# Pin mitmproxy's config dir to the bind-mount location of its CA
# regardless of which user mitmdump runs as. In the legacy
# four-sidecar setup (Dockerfile.egress, USER mitmproxy) this
# resolved naturally to `~mitmproxy/.mitmproxy`. In the PRD 0024
# bundle (USER root) `~root/.mitmproxy` is empty, so without this
# flag mitmdump would generate a fresh CA on the wrong path and
# the agent's installed trust anchor would no longer match the
# bumped leaf certs.
CONFDIR=/home/mitmproxy/.mitmproxy
CONFDIR_FLAG="--set confdir=$CONFDIR"

MODE="--mode regular@9099"
if [ -n "$EGRESS_UPSTREAM_PROXY" ]; then
  MODE="--mode upstream:$EGRESS_UPSTREAM_PROXY --listen-port 9099"
fi

# Bind address. Docker backend wants `0.0.0.0` (agent dials egress
# directly via the docker network alias). A VM backend uses
# EGRESS_LISTEN_HOST when a non-default binding is needed.
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

# Scope the proxy env to this process tree only. In the bundle
# image (PRD 0024) multiple daemons share one container — setting
# HTTPS_PROXY at the container level would route git-gate's git
# pushes through an upstream proxy unintentionally. Setting them
# here means only mitmdump's subprocess inherits them.
if [ -n "$EGRESS_UPSTREAM_PROXY" ]; then
  export HTTPS_PROXY="$EGRESS_UPSTREAM_PROXY"
  export HTTP_PROXY="$EGRESS_UPSTREAM_PROXY"
  export NO_PROXY="localhost,127.0.0.1"
fi

exec mitmdump $CONFDIR_FLAG $MODE $LISTEN_HOST_FLAG $TRUST_FLAG -s /app/egress_addon.py
