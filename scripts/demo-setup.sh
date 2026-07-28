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

if ! docker info >/dev/null 2>&1; then
  echo "demo-setup: docker daemon not reachable" >&2
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

# Build the image graph quietly so the recorded run shows only the
# bottle launch and the four probes, not BuildKit progress.
node_base_image=$(
  python3 -c \
    'import json; print(json.load(open("image-build-args.json"))["NODE_BASE_IMAGE"])'
)
python_base_image=$(
  python3 -c \
    'import json; print(json.load(open("image-build-args.json"))["PYTHON_BASE_IMAGE"])'
)
docker build -q \
  --build-arg "NODE_BASE_IMAGE=$node_base_image" \
  -f bot_bottle/contrib/claude/Dockerfile \
  -t bot-bottle-claude:latest . >/dev/null 2>&1 || true
docker build -q \
  --build-arg "PYTHON_BASE_IMAGE=$python_base_image" \
  -f Dockerfile.gateway \
  -t bot-bottle-gateway:latest . >/dev/null 2>&1 || true
