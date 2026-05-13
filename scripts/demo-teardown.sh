#!/usr/bin/env bash
# Undo what demo-setup.sh did. Restores any pre-existing
# claude-bottle.json, removes the dummy SSH identity. Idempotent.

set -euo pipefail

cd "$(dirname "$0")/.."

rm -f claude-bottle.json
if [ -f claude-bottle.json.demo-backup ]; then
  mv claude-bottle.json.demo-backup claude-bottle.json
fi

rm -rf "$HOME/.cache/claude-bottle-demo"
