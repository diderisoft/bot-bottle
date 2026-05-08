#!/usr/bin/env bash
# Unit: pipelock naming helpers (container_name, proxy_url, proxy_host_port).
TEST_NAME="pipelock_naming"

. "$(dirname "$0")/../lib/common.sh"
# shellcheck source=../../lib/log.sh
. "${REPO_ROOT}/lib/log.sh"
# shellcheck source=../../lib/pipelock.sh
. "${REPO_ROOT}/lib/pipelock.sh"

assert_eq "claude-bottle-pipelock-foo"        "$(pipelock_container_name foo)"          "container_name simple slug"
assert_eq "claude-bottle-pipelock-some-slug"  "$(pipelock_container_name some-slug)"    "container_name with hyphens"

# proxy_url and proxy_host_port use whatever CLAUDE_BOTTLE_PIPELOCK_PORT
# is at source time. We sourced with default (8888).
assert_eq "http://claude-bottle-pipelock-foo:8888" "$(pipelock_proxy_url foo)"        "proxy_url default port"
assert_eq "claude-bottle-pipelock-foo:8888"        "$(pipelock_proxy_host_port foo)"  "proxy_host_port default port"

# Both helpers should fail loudly without a slug (the `${1:?...}` guards).
assert_exit_nonzero "container_name: missing slug" bash -c '. "'"${REPO_ROOT}"'/lib/log.sh"; . "'"${REPO_ROOT}"'/lib/pipelock.sh"; pipelock_container_name'
assert_exit_nonzero "proxy_url: missing slug"      bash -c '. "'"${REPO_ROOT}"'/lib/log.sh"; . "'"${REPO_ROOT}"'/lib/pipelock.sh"; pipelock_proxy_url'

test_summary
