#!/usr/bin/env bash
# Human-runnable demo wrapper. Stages the demo bottle/agent and dummy
# identity (see scripts/demo-setup.sh), launches `bot-bottle start demo`
# interactively, then restores prior state. The recorded GIF
# (docs/demo.gif) goes through the same flow via docs/demo.tape.
#
# Once attached to claude inside the bottle, use the `!` prefix to run
# bash directly — e.g.
#   ! curl --proxy "$HTTPS_PROXY" -sw 'status=%{http_code}\n' \
#       -o /dev/null http://example.com/
# returns 403 because example.com is not on the bottle's allowlist.

set -euo pipefail

cd "$(dirname "$0")/.."

if [ -z "${BOT_BOTTLE_CLAUDE_OAUTH_TOKEN:-}" ]; then
  cat <<'EOF' >&2
demo: BOT_BOTTLE_CLAUDE_OAUTH_TOKEN is unset. The bottle launches claude,
which needs the token to authenticate. Set it in your shell env (e.g.
~/.zshrc) — see README §Auth — then re-run.
EOF
  exit 1
fi

bash scripts/demo-setup.sh
trap 'bash scripts/demo-teardown.sh' EXIT

# Pinned to the Apple Container backend: the Docker backend cannot
# reach its own orchestrator (published port lands on an internal-only
# network), and the demo should run the same way on every host rather
# than following the host default (Firecracker on KVM Linux).
BOT_BOTTLE_BACKEND=macos-container bot-bottle start demo
