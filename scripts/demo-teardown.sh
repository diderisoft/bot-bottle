#!/usr/bin/env bash
# Undo what demo-setup.sh did: remove the installed demo bottle/agent,
# restore whatever they displaced, drop the dummy SSH identity.
#
# Idempotent, and deliberately not `set -e` on the restore path — this
# runs from an EXIT trap, so a partial setup (or a second invocation)
# must still put back everything it can rather than bailing on the
# first missing file.

set -uo pipefail

cd "$(dirname "$0")/.."

config_root="${BOT_BOTTLE_ROOT:-$HOME/.bot-bottle}"

# Remove our copy, then restore the displaced original if there was
# one. Order matters: the backup can only move back once the demo file
# is out of the way.
#
# The `cmp` guard is what makes a second run safe. Removing $dest
# unconditionally would delete the user's own file on the second
# invocation — the first run has already restored it by then, and from
# teardown's point of view a restored original is indistinguishable
# from an installed demo file except by content.
uninstall_demo_file() {
  src=$1
  dest=$2
  if [ -e "$dest" ] && cmp -s "$src" "$dest"; then
    rm -f "$dest"
  fi
  if [ -e "$dest.demo-backup" ]; then
    mv "$dest.demo-backup" "$dest"
  fi
}

uninstall_demo_file scripts/demo/bottle.md "$config_root/bottles/demo.md"
uninstall_demo_file scripts/demo/agent.md "$config_root/agents/demo.md"

rm -rf "$HOME/.cache/bot-bottle-demo"
