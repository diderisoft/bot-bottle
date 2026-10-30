#!/usr/bin/env bash
# Phone approvals demo (PRD 0094): pi hits a host its bottle doesn't allow,
# asks for it, and you answer from your phone (or the terminal).
#
#   scripts/demo-phone-approvals.sh             launch pi from your phone
#   scripts/demo-phone-approvals.sh --terminal  launch pi here, in a scratch
#                                               repo (adds "Allow for project")
#
# Checks the host is ready, prints the prompt to give pi, then watches for
# pi's request and reports when it is answered. Nothing is installed or
# changed; --terminal's scratch repo is removed on exit.

set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
root="${BOT_BOTTLE_ROOT:-$HOME/.bot-bottle}"
bb() { "$repo/cli.py" "$@"; }

mode=phone
case "${1:-}" in
  "") ;;
  --terminal) mode=terminal ;;
  -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "usage: $0 [--terminal]" >&2; exit 2 ;;
esac

# wttr.in is on no bottle's allowlist, so pi's first try is blocked.
prompt="What's the weather in Boston right now? Run: curl -s 'https://wttr.in/Boston?format=3'
If that's blocked, use the egress-allow tool to ask for GET access to wttr.in
(say it's for a weather check), then run the curl again and tell me the result."

say() { printf '\033[1m%s\033[0m\n' "$*"; }
fail() { printf 'demo: %s\n' "$*" >&2; exit 1; }

# -- preflight ----------------------------------------------------------------

bb status >/dev/null 2>&1 || fail "the bot-bottle stack isn't running; run: $repo/cli.py up --build"
[ -f "$root/agents/pi.md" ] || fail "no pi agent; run: $repo/cli.py setup"
if [ "$mode" = phone ]; then
  python3 - "$root" <<'EOF' \
    || fail "this host isn't paired; run: $repo/cli.py pair --console-url URL"
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
try:
    console = json.loads((root / "console.json").read_text())
    remote = json.loads((root / "remote.json").read_text())
except (OSError, ValueError):
    sys.exit(1)
sys.exit(0 if console.get("url") and remote.get("enabled") is True else 1)
EOF
fi

# -- watch for pi's request and its answer --------------------------------------

watch_requests() {
  PYTHONPATH="$repo" python3 - <<'EOF'
import time
from bot_bottle.approvals import Request
from bot_bottle.contract.hostctl import Settings
from bot_bottle.control.client import OrchestratorClient, discover_orchestrator_url

client = OrchestratorClient(discover_orchestrator_url(Settings()))
seen: dict[str, Request] = {}
print("watching for pi's request (Ctrl-C to stop)…", flush=True)
while True:
    try:
        pending = {r.id: r for r in map(lambda raw: Request.from_dict(dict(raw)),
                                        client.supervise_pending())}
    except Exception as e:  # the orchestrator restarted, or isn't up yet
        print(f"  (couldn't ask the orchestrator: {e}; retrying)", flush=True)
        time.sleep(5)
        continue
    for rid, request in pending.items():
        if rid not in seen:
            print(f"\n→ {request.summary()}\n  why: {request.justification or '(none given)'}"
                  "\n  answer it on your phone (or Ctrl-\\ in pi's terminal)", flush=True)
    for rid, request in seen.items():
        if rid not in pending:
            print(f"✓ answered: {request.summary()} — pi carries on", flush=True)
    seen = pending
    time.sleep(1)
EOF
}

if [ "$mode" = terminal ]; then
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  git -C "$work" init -q
  printf '# weather demo\n' > "$work/README.md"
  git -C "$work" add README.md
  git -C "$work" -c user.name=demo -c user.email=demo@localhost commit -qm init
  say "pi will start in a scratch repo. Paste this as its first message:"
  printf '\n%s\n\n' "$prompt"
  say "When pi asks, the status bar turns copper: press Ctrl-\\ to answer."
  say "\"Allow for project\" writes wttr.in into $work/.bot-bottle/bottles/."
  read -r -p "Press Enter to start pi… " _
  cd "$work"
  bb pi
  exit 0
fi

say "On your phone:"
cat <<EOF
  1. Open the console and sign in with your passkey.
  2. Launch an agent: pi, on this host. Paste this as the prompt:

$prompt

  3. pi tries the curl, is blocked, and asks for wttr.in. An "Approval
     needed" card appears on pi's page. Tap Allow (Face ID / Touch ID),
     or Deny.
  4. pi retries the curl and tells you the weather (or that it was denied).

EOF
watch_requests
