# Tests

Plain-bash test suite. No framework dependency — assertions are tiny
helpers in `tests/lib/assert.sh` and the runner is a shell script.
The unit tests run anywhere bash + jq are present; the integration
tests need Docker and skip cleanly otherwise.

## Layout

```
tests/
  run_tests.sh                   # entry point
  lib/
    assert.sh                    # assert_eq, assert_contains, assert_match, ...
    common.sh                    # sources assert + fixtures, sets REPO_ROOT
    fixtures.sh                  # JSON manifest builders
  unit/                          # no docker; fast
    test_pipelock_naming.sh
    test_pipelock_classify.sh
    test_pipelock_allowlist.sh
    test_pipelock_yaml.sh
  integration/                   # require docker
    test_pipelock_image.sh
    test_pipelock_sidecar_smoke.sh
    test_dry_run_plan.sh
    test_orphan_cleanup.sh
```

## Running

```bash
tests/run_tests.sh                   # everything
tests/run_tests.sh unit               # unit only
tests/run_tests.sh integration        # integration only
tests/run_tests.sh tests/unit/test_pipelock_yaml.sh   # one file
```

Each test file exits 0 on pass, 1 on fail. The runner aggregates and
prints a one-line summary.

## What the integration tests cover

These are versions of the smoke tests run during PR #1:

- `test_pipelock_image.sh` — the pinned digest is reachable, ENTRYPOINT
  is `/pipelock`, and `CMD` includes `run`. Catches a pipelock release
  that bumps the argv shape.
- `test_pipelock_sidecar_smoke.sh` — `docker create` + `docker cp` the
  generated YAML to `/etc/pipelock.yaml` + `docker start`, then probe
  `/health`. Catches the YAML-path bug we hit (the image is distroless,
  so `/etc/pipelock/` does not exist) and YAML structural breakage.
- `test_dry_run_plan.sh` — `cli.sh start --dry-run` shows the resolved
  egress allowlist and creates zero docker resources.
- `test_orphan_cleanup.sh` — when the sidecar fails to start (bogus
  image digest), the EXIT trap removes both the internal and egress
  networks. Catches regressions in trap-installation ordering.

## What's NOT covered

- `lib/ssh.sh` end-to-end (would need a fake SSH host inside the
  container; high effort for v1).
- A live SSH-through-pipelock tunnel against a real Tailscale-style
  internal IP.
- DLP false-positive measurements.
- TLS handling / cert pinning behavior.

## Adding a test

1. Pick `unit/` (no docker) or `integration/` (docker required).
2. Name it `test_<topic>.sh`. Make it executable: `chmod +x`.
3. Start with the boilerplate the existing files use:
   ```bash
   #!/usr/bin/env bash
   TEST_NAME="<topic>"
   . "$(dirname "$0")/../lib/common.sh"
   . "${REPO_ROOT}/lib/log.sh"
   . "${REPO_ROOT}/lib/<file-under-test>.sh"
   # ...assert_eq / assert_contains / ...
   test_summary
   ```
4. For integration tests: call `skip_test_if_no_docker` after the
   boilerplate and ensure your trap cleans up any docker resources you
   create.
