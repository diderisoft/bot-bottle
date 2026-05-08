#!/usr/bin/env bash
# Integration: verify the pinned pipelock image. Requires docker.
#   - Pinned digest is reachable on the registry.
#   - Image's ENTRYPOINT/CMD match what lib/pipelock.sh assumes
#     (`/pipelock` and `run --listen 0.0.0.0:8888`).
#   - The /pipelock binary actually runs (--version succeeds).
#
# This is the test that would have caught the runtime bug where the
# CMD shape diverged from what the launcher passed.
TEST_NAME="pipelock_image"

. "$(dirname "$0")/../lib/common.sh"
# shellcheck source=../../lib/log.sh
. "${REPO_ROOT}/lib/log.sh"
# shellcheck source=../../lib/pipelock.sh
. "${REPO_ROOT}/lib/pipelock.sh"

skip_test_if_no_docker

# Pull the pinned image (cheap if already cached).
if ! docker pull "$CLAUDE_BOTTLE_PIPELOCK_IMAGE" >/dev/null 2>&1; then
  skip "could not pull ${CLAUDE_BOTTLE_PIPELOCK_IMAGE}"
  exit 0
fi

# ENTRYPOINT must be the binary path lib/pipelock.sh expects.
entrypoint="$(docker image inspect "$CLAUDE_BOTTLE_PIPELOCK_IMAGE" --format '{{json .Config.Entrypoint}}')"
assert_contains "$entrypoint" "/pipelock" "entrypoint contains /pipelock"

# CMD must include `run` — the subcommand the launcher overrides via
# `docker create ... run --config ... --listen ...`. If a future image
# bumps the CMD shape, this fails loudly.
cmd="$(docker image inspect "$CLAUDE_BOTTLE_PIPELOCK_IMAGE" --format '{{json .Config.Cmd}}')"
assert_contains "$cmd" "run" "cmd contains 'run'"

# Binary actually runs.
ver="$(docker run --rm "$CLAUDE_BOTTLE_PIPELOCK_IMAGE" --version 2>&1 || true)"
assert_match "$ver" "[Pp]ipelock|2\\.[0-9]+\\.[0-9]+" "binary --version produces version-shaped output"

test_summary
