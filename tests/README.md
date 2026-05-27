# Tests

Plain-Python test suite using stdlib `unittest`. No external
dependencies. Unit tests run anywhere Python 3 is present; integration
tests need Docker and skip cleanly otherwise.

## Layout

```
tests/
  fixtures.py                       # JSON manifest builders (shared)
  _docker.py                        # docker-availability skip helper (shared)
  unit/
    test_pipelock_classify.py
    test_pipelock_allowlist.py
    test_pipelock_yaml.py
    test_manifest_runtime.py
  integration/
    test_pipelock_sidecar_smoke.py
    test_dry_run_plan.py
    test_orphan_cleanup.py
  canaries/
    test_pipelock_image.py          # opt-in; see below
```

Classification falls out of the directory — no hand-maintained list to
keep in sync.

## Running

```bash
python -m unittest discover -t . -s tests/unit -v         # unit only
python -m unittest discover -t . -s tests/integration -v  # integration only
python -m unittest discover -t . -s tests -v              # both (recursive)
python -m unittest tests.unit.test_pipelock_yaml          # one file
```

Discovery is invoked with `-t .` (top-level dir = repo root) so the
`claude_bottle` package on `sys.path` resolves correctly.

## What the integration tests cover

- `test_dry_run_plan.py` — `cli.py start --dry-run --format=json` emits
  a structured plan that contains the resolved egress allowlist and
  the bottle's runtime, and creates zero Docker resources.
- `test_orphan_cleanup.py` — `network_remove` is idempotent against
  missing resources, so the EXIT trap can call it unconditionally.
- `test_sidecar_bundle_image.py` — builds Dockerfile.sidecars and
  probes that pipelock / gitleaks / mitmdump / supervise are all
  reachable inside the bundle.
- `test_sidecar_bundle_compose.py` — end-to-end compose-up of an
  agent + bundle pair; verifies the agent reaches the bundle via
  the legacy network aliases.

## Canaries

`tests/canaries/` holds upstream-regression checks (e.g. the pinned
pipelock digest's binary still runs). These are gated on
`CLAUDE_BOTTLE_RUN_CANARIES=1` and not part of the per-push suite.
They're invoked by the scheduled `canaries` workflow.

```bash
CLAUDE_BOTTLE_RUN_CANARIES=1 python -m unittest discover -t . -s tests/canaries -v
```

## What's NOT covered

- `claude_bottle/ssh.py` end-to-end (would need a fake SSH host inside
  the container).
- A live SSH-through-pipelock tunnel against a real Tailscale-style IP.
- DLP false-positive measurements.
- TLS handling / cert pinning behavior.

## Adding a test

1. Pick the directory: `tests/unit/` for a pure unit test,
   `tests/integration/` for one that needs Docker.
2. Filename: `test_<topic>.py`.
3. Boilerplate:
   ```python
   import unittest

   from claude_bottle.<module> import <symbol>

   class TestThing(unittest.TestCase):
       def test_x(self):
           ...

   if __name__ == "__main__":
       unittest.main()
   ```
4. For Docker-dependent tests, decorate the class with
   `@skip_unless_docker()` from `tests._docker`.
