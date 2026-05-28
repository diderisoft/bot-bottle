#!/usr/bin/env bash
# Undo what demo-setup.sh did. Restores any pre-existing
# bot-bottle.json, removes the dummy SSH identity. Idempotent.

set -euo pipefail

cd "$(dirname "$0")/.."

rm -f bot-bottle.json
if [ -f bot-bottle.json.demo-backup ]; then
  mv bot-bottle.json.demo-backup bot-bottle.json
fi

rm -rf "$HOME/.cache/bot-bottle-demo"
