#!/usr/bin/env bash
# Integration: full sidecar smoke test. Boots a pipelock container the
# same way cli.sh does (docker create + docker cp YAML + docker start),
# then probes /health. Catches regressions in:
#   - the YAML-cp path (the /etc/pipelock.yaml vs /etc/pipelock/ bug)
#   - argv shape (the `run --listen 0.0.0.0:N` invocation)
#   - YAML structural validity (pipelock would refuse to start on a bad config)
TEST_NAME="pipelock_sidecar_smoke"

. "$(dirname "$0")/../lib/common.sh"
# shellcheck source=../../lib/log.sh
. "${REPO_ROOT}/lib/log.sh"
# shellcheck source=../../lib/pipelock.sh
. "${REPO_ROOT}/lib/pipelock.sh"

skip_test_if_no_docker

# Use a distinct name so concurrent runs don't collide.
name="cb-test-pipelock-smoke-$$"
work_dir="$(mktemp -d)"
yaml="${work_dir}/pipelock.yaml"

cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  rm -rf "$work_dir"
}
trap cleanup EXIT

# Generate a real config from a fixture manifest.
m="$(write_fixture fixture_minimal)"
pipelock_write_yaml "$m" dev "$yaml"
rm -f "$m"

# Same lifecycle as lib/pipelock.sh's pipelock_start, minus the
# network-attach steps (we just need a port we can curl).
docker create --name "$name" -p 0:8888 \
  "$CLAUDE_BOTTLE_PIPELOCK_IMAGE" \
  run --config /etc/pipelock.yaml --listen "0.0.0.0:8888" \
  >/dev/null 2>&1 \
  || { _fail "docker create failed"; test_summary; }

# This is the exact cp path that broke before — guard against
# regressing to a /etc/pipelock/ subdirectory destination.
if ! docker cp "$yaml" "${name}:/etc/pipelock.yaml" >/dev/null 2>&1; then
  _fail "docker cp to /etc/pipelock.yaml failed (parent dir must already exist in image)"
  test_summary
fi

if ! docker start "$name" >/dev/null 2>&1; then
  _fail "docker start failed; check that argv 'run --listen 0.0.0.0:8888' still matches image"
  test_summary
fi

# Find the host-side port docker mapped 8888 to.
hostport="$(docker port "$name" 8888 2>/dev/null | head -1 | awk -F: '{print $NF}')"
if [ -z "$hostport" ]; then
  _fail "could not determine published port" "docker port output: $(docker port "$name" 2>&1)"
  test_summary
fi

# Wait up to 15 seconds for /health to come up.
healthy=0
for _ in $(seq 1 15); do
  if curl -fsS "http://127.0.0.1:${hostport}/health" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 1
done

if [ "$healthy" -eq 1 ]; then
  _pass "sidecar /health responded"
else
  _fail "sidecar /health did not respond within 15s" "logs:" "$(docker logs "$name" 2>&1 | tail -20)"
  test_summary
fi

# Body should mention the version we pinned. We don't pin the exact
# version string here because the digest we test against is one
# release; the next release will change the version field but should
# keep the schema. Keep the assertion at "field is present and has
# a numeric-dotted shape".
body="$(curl -fsS "http://127.0.0.1:${hostport}/health" 2>&1)"
assert_contains "$body" '"status":"healthy"' "/health body status:healthy"
assert_match    "$body" '"version":"[0-9]+\.[0-9]+\.[0-9]+"' "/health body has version field"

test_summary
