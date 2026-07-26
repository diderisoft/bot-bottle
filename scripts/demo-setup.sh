#!/usr/bin/env bash
# Prepare the working directory to run the recorded demo via cli.py:
#   - back up any existing bot-bottle.json so the user's real config
#     isn't clobbered
#   - install bot-bottle.demo.json as bot-bottle.json
#   - create a dummy SSH identity at the path the demo manifest expects
#   - pre-warm the bottle + git-gate images quietly so the recording
#     doesn't spend its first 30s in BuildKit output

set -euo pipefail

cd "$(dirname "$0")/.."

if ! docker info >/dev/null 2>&1; then
  echo "demo-setup: docker daemon not reachable" >&2
  exit 1
fi

# Back up an existing local manifest (untouched if absent). Stored
# alongside the manifest with a deterministic name so teardown can
# find it without state files.
if [ -f bot-bottle.json ]; then
  cp bot-bottle.json bot-bottle.json.demo-backup
fi
cp bot-bottle.demo.json bot-bottle.json

# Dummy SSH identity — the git-gate validator wants a readable file at
# the IdentityFile path. Contents don't matter for the demo: the
# unreachable upstream means the gate never actually uses the key.
fake_key_dir="$HOME/.cache/bot-bottle-demo"
mkdir -p "$fake_key_dir"
chmod 700 "$fake_key_dir"
printf 'not-a-real-key\n' > "$fake_key_dir/fake-key"
chmod 600 "$fake_key_dir/fake-key"

# Build the image graph quietly so the recorded run shows only the
# bottle launch and the four `!` probes, not BuildKit progress.
node_base_image=$(
  python3 -c \
    'import json; print(json.load(open("image-build-args.json"))["NODE_BASE_IMAGE"])'
)
docker build -q \
  --build-arg "NODE_BASE_IMAGE=$node_base_image" \
  -f bot_bottle/contrib/claude/Dockerfile \
  -t bot-bottle-claude:latest . >/dev/null 2>&1 || true
docker build -q -f Dockerfile.git-gate -t bot-bottle-git-gate:latest . >/dev/null 2>&1 || true
