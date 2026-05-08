#!/usr/bin/env bash
# Unit: allowlist resolution — pipelock_bottle_allowlist,
# pipelock_bottle_ssh_hostnames, pipelock_bottle_ssh_ip_cidrs,
# pipelock_bottle_ssh_trusted_domains, pipelock_effective_allowlist.
TEST_NAME="pipelock_allowlist"

. "$(dirname "$0")/../lib/common.sh"
# shellcheck source=../../lib/log.sh
. "${REPO_ROOT}/lib/log.sh"
# shellcheck source=../../lib/pipelock.sh
. "${REPO_ROOT}/lib/pipelock.sh"

# --- bottle_allowlist (egress.allowlist parsing) ---

m="$(write_fixture fixture_with_egress)"
out="$(pipelock_bottle_allowlist "$m" dev)"
assert_contains "$out" "github.com"          "bottle_allowlist: github.com present"
assert_contains "$out" "gitlab.com"          "bottle_allowlist: gitlab.com present"
assert_contains "$out" "registry.npmjs.org"  "bottle_allowlist: npmjs present"
rm -f "$m"

m="$(write_fixture fixture_minimal)"
out="$(pipelock_bottle_allowlist "$m" dev)"
assert_eq "" "$out" "bottle_allowlist: empty when no egress block"
rm -f "$m"

# --- ssh hostnames + classification ---

m="$(write_fixture fixture_with_ssh)"
hosts="$(pipelock_bottle_ssh_hostnames "$m" dev)"
assert_contains "$hosts" "192.0.2.10" "ssh_hostnames: ipv4 included"
assert_contains "$hosts" "github.com"    "ssh_hostnames: hostname included"

cidrs="$(pipelock_bottle_ssh_ip_cidrs "$m" dev)"
assert_contains "$cidrs"     "192.0.2.10/32" "ssh_ip_cidrs: ipv4 emitted as /32"
assert_not_contains "$cidrs" "github.com"       "ssh_ip_cidrs: hostname not in cidr list"

trusted="$(pipelock_bottle_ssh_trusted_domains "$m" dev)"
assert_contains "$trusted"     "github.com"        "ssh_trusted_domains: hostname present"
assert_not_contains "$trusted" "192.0.2.10"     "ssh_trusted_domains: ipv4 not present"
rm -f "$m"

# --- effective_allowlist union (defaults + bottle.allowlist + ssh.Hostname) ---

# Combine egress + ssh fixtures into one manifest.
combined="$(mktemp)"
cat > "$combined" <<'JSON'
{
  "bottles": {
    "dev": {
      "egress": { "allowlist": ["registry.npmjs.org"] },
      "ssh": [
        { "Host": "ts", "IdentityFile": "/dev/null", "Hostname": "192.0.2.10", "User": "git", "Port": 30009 },
        { "Host": "gh", "IdentityFile": "/dev/null", "Hostname": "github.com",    "User": "git", "Port": 22 }
      ]
    }
  },
  "agents": { "demo": { "skills": [], "prompt": "", "bottle": "dev" } }
}
JSON

eff="$(pipelock_effective_allowlist "$combined" dev)"
assert_contains "$eff" "api.anthropic.com"  "effective: baked-in default present"
assert_contains "$eff" "registry.npmjs.org" "effective: bottle egress entry present"
assert_contains "$eff" "192.0.2.10"      "effective: ssh ipv4 hostname present"
assert_contains "$eff" "github.com"         "effective: ssh hostname present"

# Ensure dedup + sort: count lines, then count unique lines, expect equal.
total="$(printf '%s\n' "$eff" | wc -l | tr -d ' ')"
uniq="$(printf '%s\n' "$eff" | sort -u | wc -l | tr -d ' ')"
assert_eq "$total" "$uniq" "effective: deduplicated"

rm -f "$combined"

# --- non-string entry rejection ---

bad="$(mktemp)"
cat > "$bad" <<'JSON'
{
  "bottles": { "dev": { "egress": { "allowlist": ["github.com", 42] } } },
  "agents": { "demo": { "skills": [], "prompt": "", "bottle": "dev" } }
}
JSON

assert_exit_nonzero "bottle_allowlist: rejects non-string entry" \
  bash -c '. "'"${REPO_ROOT}"'/lib/log.sh"; . "'"${REPO_ROOT}"'/lib/pipelock.sh"; pipelock_bottle_allowlist "'"$bad"'" dev'
rm -f "$bad"

test_summary
