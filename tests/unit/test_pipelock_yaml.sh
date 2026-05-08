#!/usr/bin/env bash
# Unit: pipelock_write_yaml — produces a YAML config containing the
# expected top-level keys and per-bottle entries. We don't fully parse
# YAML (no yq dependency); we grep for content shape.
TEST_NAME="pipelock_yaml"

. "$(dirname "$0")/../lib/common.sh"
# shellcheck source=../../lib/log.sh
. "${REPO_ROOT}/lib/log.sh"
# shellcheck source=../../lib/pipelock.sh
. "${REPO_ROOT}/lib/pipelock.sh"

out_dir="$(mktemp -d)"
cleanup() { rm -rf "$out_dir"; }
trap cleanup EXIT

# --- minimal bottle (no egress, no ssh): only api_allowlist defaults ---

m_min="$(write_fixture fixture_minimal)"
yaml_min="${out_dir}/min.yaml"
pipelock_write_yaml "$m_min" dev "$yaml_min"

content="$(cat "$yaml_min")"
assert_contains "$content" "mode: strict"             "min: mode strict"
assert_contains "$content" "enforce: true"            "min: enforce true"
assert_contains "$content" "api_allowlist:"           "min: api_allowlist block"
assert_contains "$content" "api.anthropic.com"        "min: anthropic baked default"
assert_contains "$content" "raw.githubusercontent.com" "min: github raw baked default"
assert_contains "$content" "forward_proxy:"           "min: forward_proxy block"
assert_contains "$content" "enabled: true"            "min: forward_proxy enabled"
assert_contains "$content" "dlp:"                     "min: dlp block"
assert_contains "$content" "include_defaults: true"   "min: dlp include_defaults"
assert_contains "$content" "scan_env: true"           "min: dlp scan_env"
# No ssh entries in the manifest, so neither ssrf nor trusted_domains
# blocks should be emitted.
assert_not_contains "$content" "trusted_domains:" "min: no trusted_domains"
assert_not_contains "$content" "ssrf:"            "min: no ssrf block"

rm -f "$m_min"

# --- ssh bottle: trusted_domains for hostname, ssrf.ip_allowlist for ipv4 ---

m_ssh="$(write_fixture fixture_with_ssh)"
yaml_ssh="${out_dir}/ssh.yaml"
pipelock_write_yaml "$m_ssh" dev "$yaml_ssh"

content="$(cat "$yaml_ssh")"
assert_contains "$content" "trusted_domains:"   "ssh: trusted_domains block emitted"
assert_contains "$content" "github.com"         "ssh: hostname in trusted_domains (or allowlist)"
assert_contains "$content" "ssrf:"              "ssh: ssrf block emitted"
assert_contains "$content" "ip_allowlist:"      "ssh: ip_allowlist key under ssrf"
assert_contains "$content" "192.0.2.10/32"   "ssh: ipv4 host emitted as /32"
# Belt-and-suspenders: the ipv4 host should also be in api_allowlist
# (strict mode requires both).
assert_contains "$content" "192.0.2.10"      "ssh: ipv4 host in api_allowlist too"

rm -f "$m_ssh"

# --- secret hygiene: env values from the manifest never enter the YAML ---

m_secret="$(mktemp)"
cat > "$m_secret" <<'JSON'
{
  "bottles": {
    "dev": {
      "env": {
        "MY_SECRET": "literal-value-should-not-appear",
        "ANOTHER":   "?prompt-message"
      },
      "egress": { "allowlist": ["github.com"] }
    }
  },
  "agents": { "demo": { "skills": [], "prompt": "", "bottle": "dev" } }
}
JSON
yaml_sec="${out_dir}/secret.yaml"
pipelock_write_yaml "$m_secret" dev "$yaml_sec"
content="$(cat "$yaml_sec")"
assert_not_contains "$content" "literal-value-should-not-appear" "secret: literal env value not leaked"
assert_not_contains "$content" "MY_SECRET"                       "secret: env var name not leaked"
assert_not_contains "$content" "prompt-message"                  "secret: prompt sentinel not leaked"
rm -f "$m_secret"

# --- file mode is 600 ---
mode="$(stat -f '%p' "$yaml_min" 2>/dev/null || stat -c '%a' "$yaml_min")"
# macOS stat -f '%p' returns full mode like 100600; trim. Linux stat -c '%a' gives just 600.
mode="${mode: -3}"
assert_eq "600" "$mode" "yaml file mode is 600"

test_summary
