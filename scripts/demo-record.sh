#!/usr/bin/env bash
# Record docs/demo.gif via VHS. Runs setup, invokes `vhs docs/demo.tape`,
# always tears down. Requires `vhs` (brew install vhs).

set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v vhs >/dev/null 2>&1; then
  echo "demo-record: vhs not found on PATH (brew install vhs)" >&2
  exit 1
fi

if [ -z "${BOT_BOTTLE_CLAUDE_OAUTH_TOKEN:-}" ]; then
  echo "demo-record: BOT_BOTTLE_CLAUDE_OAUTH_TOKEN is unset; claude inside the bottle will not auth" >&2
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "demo-record: ffmpeg not found on PATH (brew install ffmpeg) — needed for decimation pass" >&2
  exit 1
fi

bash scripts/demo-setup.sh
trap 'bash scripts/demo-teardown.sh' EXIT

vhs docs/demo.tape

# VHS records in real time, which leaves long static stretches while
# the bottle launches and commands wait for output. Run mpdecimate to
# drop duplicate consecutive frames (TUI dead time) and re-time at
# 12 fps. tpad clones the final frame for 4s so the gitleaks
# rejection on the last beat dwells long enough to read on each GIF
# loop. Re-encode through a 64-color palette to keep the file small.
tmp=$(mktemp -d)
trap 'bash scripts/demo-teardown.sh; rm -rf "$tmp"' EXIT
cp docs/demo.gif "$tmp/raw.gif"
ffmpeg -y -i "$tmp/raw.gif" \
  -vf "mpdecimate,setpts=N/12/TB,tpad=stop_duration=4:stop_mode=clone,scale=960:-1:flags=lanczos,palettegen=max_colors=64" \
  "$tmp/palette.png" -loglevel error
ffmpeg -y -i "$tmp/raw.gif" -i "$tmp/palette.png" \
  -lavfi "mpdecimate,setpts=N/12/TB,tpad=stop_duration=4:stop_mode=clone,scale=960:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5" \
  docs/demo.gif -loglevel error

echo "demo-record: wrote $(ls -lh docs/demo.gif | awk '{print $5}') ($(ffprobe -v error -show_entries stream=duration -of default=nk=1:nw=1 docs/demo.gif | cut -d. -f1)s)"
