#!/usr/bin/env bash
# Integration: the cleanup primitives the start-flow trap depends on
# are idempotent. The original orphan-network bug was a trap-ordering
# issue (cleanup_all installed AFTER networks were created); the fix
# moved the install earlier. The trap is only safe if the helpers it
# calls — network_remove, pipelock_stop — are no-ops against
# already-missing or never-existed resources. We test that here.
#
# (The full end-to-end "cli.sh dies mid-run, networks gone" flow needs
# a TTY and is documented as a manual verification step in tests/README.md.)
TEST_NAME="orphan_cleanup"

. "$(dirname "$0")/../lib/common.sh"
# shellcheck source=../../lib/log.sh
. "${REPO_ROOT}/lib/log.sh"
# shellcheck source=../../lib/docker.sh
. "${REPO_ROOT}/lib/docker.sh"
# shellcheck source=../../lib/network.sh
. "${REPO_ROOT}/lib/network.sh"
# shellcheck source=../../lib/pipelock.sh
. "${REPO_ROOT}/lib/pipelock.sh"

skip_test_if_no_docker

slug="cb-test-orphan-$$"
internal_name=""
egress_name=""

cleanup() {
  for n in "$internal_name" "$egress_name"; do
    [ -n "$n" ] && docker network rm "$n" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

# 1. network_remove against a name that doesn't exist returns 0
#    (the trap can call it eagerly without crashing on the first run
#    where the network was never created).
assert_exit_zero "network_remove: missing network is a no-op" \
  network_remove "claude-bottle-net-${slug}-does-not-exist"

# 2. Create both networks the way cli.sh does, then remove them with
#    network_remove. Both should succeed and the networks should be
#    gone afterwards.
internal_name="$(network_create_internal "$slug")"
egress_name="$(network_create_egress "$slug")"

assert_match "$(docker network ls --format '{{.Name}}')" "^${internal_name}$" \
  "internal network was created"
assert_match "$(docker network ls --format '{{.Name}}')" "^${egress_name}$" \
  "egress network was created"

assert_exit_zero "network_remove: removes existing internal network" \
  network_remove "$internal_name"
assert_exit_zero "network_remove: removes existing egress network" \
  network_remove "$egress_name"

nets_after="$(docker network ls --format '{{.Name}}')"
assert_not_contains "$nets_after" "$internal_name" "internal network gone after removal"
assert_not_contains "$nets_after" "$egress_name"   "egress network gone after removal"

# 3. Removing a second time is still safe — the trap may run after a
#    clean exit, where the resources are already gone.
assert_exit_zero "network_remove: idempotent on already-removed internal" \
  network_remove "$internal_name"
assert_exit_zero "network_remove: idempotent on already-removed egress" \
  network_remove "$egress_name"

# 4. pipelock_stop against a slug whose sidecar was never started must
#    also be a no-op — same reason.
assert_exit_zero "pipelock_stop: missing sidecar is a no-op" \
  pipelock_stop "missing-${slug}"

test_summary
