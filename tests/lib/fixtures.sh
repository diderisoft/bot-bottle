#!/usr/bin/env bash
# Manifest fixture builders. Each function prints a JSON manifest on
# stdout; callers can pipe to a temp file or pass through `write_fixture`.

if [ -n "${CLAUDE_BOTTLE_TESTS_FIXTURES_SOURCED:-}" ]; then
  return 0
fi
CLAUDE_BOTTLE_TESTS_FIXTURES_SOURCED=1

# fixture_minimal — one bottle, one agent, no env / ssh / skills.
fixture_minimal() {
  cat <<'JSON'
{
  "bottles": {
    "dev": {}
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
}

# fixture_with_egress — bottle declares an egress.allowlist.
fixture_with_egress() {
  cat <<'JSON'
{
  "bottles": {
    "dev": {
      "egress": {
        "allowlist": [
          "github.com",
          "gitlab.com",
          "registry.npmjs.org"
        ]
      }
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
}

# fixture_with_ssh — bottle has both an IPv4-literal SSH host (Tailscale
# CGNAT range) and a hostname SSH host, exercising both
# ssrf.ip_allowlist and trusted_domains code paths.
fixture_with_ssh() {
  cat <<'JSON'
{
  "bottles": {
    "dev": {
      "ssh": [
        {
          "Host": "tailscale-gitea",
          "IdentityFile": "/dev/null",
          "Hostname": "192.0.2.10",
          "User": "git",
          "Port": 30009
        },
        {
          "Host": "github",
          "IdentityFile": "/dev/null",
          "Hostname": "github.com",
          "User": "git",
          "Port": 22
        }
      ]
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
}

# write_fixture <fixture_func> — write fixture to a temp file, print
# the path. Caller must rm.
write_fixture() {
  local fn="${1:?write_fixture: missing fixture function}"
  local f
  f="$(mktemp)"
  "$fn" > "$f"
  printf '%s' "$f"
}
