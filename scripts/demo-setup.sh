#!/usr/bin/env bash
# Prepare the working directory to run the recorded demo via cli.py:
#   - back up any existing claude-bottle.json so the user's real config
#     isn't clobbered
#   - install claude-bottle.demo.json as claude-bottle.json
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
if [ -f claude-bottle.json ]; then
  cp claude-bottle.json claude-bottle.json.demo-backup
fi
cp claude-bottle.demo.json claude-bottle.json

# Dummy SSH identity — the git-gate validator wants a readable file at
# the IdentityFile path. Contents don't matter for the demo: the
# unreachable upstream means the gate never actually uses the key.
fake_key_dir="$HOME/.cache/claude-bottle-demo"
mkdir -p "$fake_key_dir"
chmod 700 "$fake_key_dir"
printf 'not-a-real-key\n' > "$fake_key_dir/fake-key"
chmod 600 "$fake_key_dir/fake-key"

# Build the image graph quietly so the recorded run shows only the
# bottle launch and the four `!` probes, not BuildKit progress.
docker build -q -t claude-bottle:latest . >/dev/null 2>&1 || true
docker build -q -f Dockerfile.git-gate -t claude-bottle-git-gate:latest . >/dev/null 2>&1 || true
