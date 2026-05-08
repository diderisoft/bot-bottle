#!/usr/bin/env bash
# Integration: cli.sh start --dry-run renders the planned shape and
# does not create any docker resources. Confirms the preflight contract
# from PRD 0001 (allowlist line in the plan, no docker side effects).
TEST_NAME="dry_run_plan"

. "$(dirname "$0")/../lib/common.sh"

skip_test_if_no_docker

work_dir="$(mktemp -d)"
manifest="${work_dir}/claude-bottle.json"

cleanup() {
  rm -rf "$work_dir"
}
trap cleanup EXIT

# Manifest with an egress.allowlist so we can grep for a known host.
cat > "$manifest" <<'JSON'
{
  "bottles": {
    "dev": {
      "egress": { "allowlist": ["example.org"] }
    }
  },
  "agents": {
    "demo": {
      "skills": [],
      "prompt": "",
      "bottle": "dev"
    }
  }
}
JSON

# Snapshot docker state before we run.
nets_before="$(docker network ls --format '{{.Name}}' | grep -c '^claude-bottle' || true)"
ctrs_before="$(docker ps -a --format '{{.Names}}'     | grep -c '^claude-bottle' || true)"

# Override HOME so the user's ~/claude-bottle.json doesn't leak in via
# manifest_resolve's home+cwd merge.
out="$(cd "$work_dir" \
  && HOME="$work_dir" CLAUDE_BOTTLE_DRY_RUN=1 \
     "${REPO_ROOT}/cli.sh" start demo 2>&1 || true)"

assert_contains "$out" "egress"             "preflight: egress line present"
# 7 baked defaults + 1 bottle entry = 8. The summary line shows the
# total count regardless of which entries fit in the visible
# "<a>, <b>, <c>, +N more" prefix, so this assertion is robust against
# alphabetical sort order changes.
assert_match    "$out" "8 hosts allowed"    "preflight: bottle entry counted in effective allowlist"
assert_contains "$out" "api.anthropic.com"  "preflight: baked default shown"
assert_contains "$out" "dry-run requested"  "dry-run banner present"
assert_not_contains "$out" "/dev/tty"       "no /dev/tty prompt reached (dry-run exited first)"

# No docker side effects.
nets_after="$(docker network ls --format '{{.Name}}' | grep -c '^claude-bottle' || true)"
ctrs_after="$(docker ps -a --format '{{.Names}}'     | grep -c '^claude-bottle' || true)"
assert_eq "$nets_before" "$nets_after" "dry-run: no claude-bottle networks created"
assert_eq "$ctrs_before" "$ctrs_after" "dry-run: no claude-bottle containers created"

test_summary
