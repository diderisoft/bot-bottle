#!/usr/bin/env bash
# Unit: _pipelock_is_ipv4_literal — the classifier that decides
# whether bottle.ssh[].Hostname goes into ssrf.ip_allowlist (IPv4
# literal) or trusted_domains (hostname).
TEST_NAME="pipelock_classify"

. "$(dirname "$0")/../lib/common.sh"
# shellcheck source=../../lib/log.sh
. "${REPO_ROOT}/lib/log.sh"
# shellcheck source=../../lib/pipelock.sh
. "${REPO_ROOT}/lib/pipelock.sh"

# Positive cases — these should be classified as IPv4 literals.
for ip in "127.0.0.1" "10.0.0.5" "192.0.2.10" "0.0.0.0" "255.255.255.255"; do
  assert_exit_zero "ipv4: ${ip}" _pipelock_is_ipv4_literal "$ip"
done

# Negative cases — hostnames, partial IPs, IPv6, and edge garbage
# should NOT match.
for hn in \
  "github.com" \
  "gitea.dideric.is" \
  "100.78.141" \
  "192.0.2.10.5" \
  "::1" \
  "fe80::1" \
  "localhost" \
  "" \
  "1.2.3.4.example.com"
do
  assert_exit_nonzero "non-ipv4: '${hn}'" _pipelock_is_ipv4_literal "$hn"
done

test_summary
