#!/usr/bin/env bash
# Stage everything the recorded demo needs, then hand off to demo.sh or
# demo-record.sh:
#   - install scripts/demo/{bottle,agent}.md into $HOME/.bot-bottle/,
#     backing up anything already sitting at those paths
#   - create a dummy SSH identity where the demo bottle's git-gate
#     expects one
#   - pre-warm the agent + gateway images quietly so the recording
#     doesn't spend its first 30s in BuildKit output
#
# Bottles can only be read from $HOME/.bot-bottle/bottles/ — a bottles/
# dir in CWD is ignored by design (manifest/index.py, PRD 0011) — so
# unlike the old throwaway manifest swap this writes into real config.
# Every write is paired with a .demo-backup so demo-teardown.sh can put
# things back exactly as they were; teardown is trapped by both
# callers and is safe to run twice.

set -euo pipefail

cd "$(dirname "$0")/.."

if ! container system status >/dev/null 2>&1; then
  echo "demo-setup: Apple Container services are not running." >&2
  echo "  Start them with: container system start" >&2
  exit 1
fi

# The tape types `bot-bottle start demo` verbatim, so the console
# script has to resolve in the recording shell. Without this guard a
# recording silently captures `command not found` instead of a bottle.
if ! command -v bot-bottle >/dev/null 2>&1; then
  echo "demo-setup: bot-bottle is not on PATH. The demo runs the real" >&2
  echo "  console script, not ./cli.py — install it first (bash install.sh," >&2
  echo "  or 'pip install -e .' into an active venv), then re-run." >&2
  exit 1
fi

config_root="${BOT_BOTTLE_ROOT:-$HOME/.bot-bottle}"
mkdir -p "$config_root/bottles" "$config_root/agents"

# Install one demo file, preserving whatever was there. The backup
# suffix is deterministic so teardown needs no state file. A stale
# backup from a killed run would be restored over the new install, so
# refuse rather than silently clobber it.
install_demo_file() {
  src=$1
  dest=$2
  # Already installed (setup run twice without an intervening
  # teardown). Backing up our own copy here would make teardown
  # "restore" it and leave the demo file in the user's config forever,
  # so treat this as a no-op.
  if [ -e "$dest" ] && cmp -s "$src" "$dest"; then
    return 0
  fi
  if [ -e "$dest.demo-backup" ]; then
    echo "demo-setup: $dest.demo-backup already exists — a previous run" >&2
    echo "  did not tear down cleanly. Inspect it, then remove or restore" >&2
    echo "  it by hand before re-running." >&2
    exit 1
  fi
  if [ -e "$dest" ]; then
    mv "$dest" "$dest.demo-backup"
  fi
  cp "$src" "$dest"
}

install_demo_file scripts/demo/bottle.md "$config_root/bottles/demo.md"
install_demo_file scripts/demo/agent.md "$config_root/agents/demo.md"

# Dummy SSH identity — the git-gate validator wants a readable file at
# the key path. Contents don't matter for the demo: the unreachable
# upstream means the gate never actually uses the key.
fake_key_dir="$HOME/.cache/bot-bottle-demo"
mkdir -p "$fake_key_dir"
chmod 700 "$fake_key_dir"
printf 'not-a-real-key\n' > "$fake_key_dir/fake-key"
chmod 600 "$fake_key_dir/fake-key"

# Report which base images are already in the Apple image store. A cold
# store isn't fatal — the launcher builds what it needs — but the first
# recorded launch then spends its opening seconds in BuildKit output
# instead of showing the bottle, so it's worth knowing before recording.
#
# Deliberately NOT pre-building here. `container build` needs the same
# --dns treatment the backend applies in
# backend/macos_container/util.py:build_image(); reproducing that in
# shell would be a second, silently-drifting copy of it. The layer
# cache already keeps a warm rebuild fast, and the old `docker build
# ... || true` pre-warm is exactly how this script kept "succeeding"
# while building a Dockerfile that had been deleted.
for image in bot-bottle-claude bot-bottle-gateway bot-bottle-orchestrator; do
  if ! container image ls 2>/dev/null | grep -q "^${image} "; then
    echo "demo-setup: note: $image not in the image store yet;" >&2
    echo "  the recording's first seconds will show it building." >&2
  fi
done
