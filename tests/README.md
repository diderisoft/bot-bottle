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
    test_egress.py
    test_egress_addon_core.py
    test_manifest_egress.py
    test_dlp_detectors.py
    test_manifest_runtime.py
    ...                             # many others; see unit/ directory
  integration/
    test_sidecar_bundle_image.py
    test_sidecar_bundle_compose.py
    test_dry_run_plan.py
    test_orphan_cleanup.py
    ...
  canaries/                         # opt-in; see below (currently empty)
```

Classification falls out of the directory — no hand-maintained list to
keep in sync.

## Running

```bash
python -m unittest discover -t . -s tests/unit -v         # unit only
python -m unittest discover -t . -s tests/integration -v  # integration only
python -m unittest discover -t . -s tests -v              # both (recursive)
python -m unittest tests.unit.test_manifest_egress        # one file
```

Discovery is invoked with `-t .` (top-level dir = repo root) so the
`bot_bottle` package on `sys.path` resolves correctly.

## What the integration tests cover

- `test_dry_run_plan.py` — `cli.py start --dry-run --format=json` emits
  a structured plan that contains the resolved egress allowlist and
  the bottle's runtime, and creates zero Docker resources.
- `test_orphan_cleanup.py` — `network_remove` is idempotent against
  missing resources, so the EXIT trap can call it unconditionally.
- `test_sidecar_bundle_image.py` — builds Dockerfile.sidecars and
  probes that gitleaks / mitmdump / supervise are all reachable
  inside the bundle.
- `test_sidecar_bundle_compose.py` — end-to-end compose-up of an
  agent + bundle pair; verifies the agent reaches the bundle via
  the legacy network aliases.

## Canaries

`tests/canaries/` holds upstream-regression checks gated on
`BOT_BOTTLE_RUN_CANARIES=1` and not part of the per-push suite.
They're invoked by the scheduled `canaries` workflow. Currently
no canaries are defined.

```bash
BOT_BOTTLE_RUN_CANARIES=1 python -m unittest discover -t . -s tests/canaries -v
```

## What's NOT covered

- `bot_bottle/ssh.py` end-to-end (would need a fake SSH host inside
  the container).
- A live SSH-through-git-gate tunnel against a real Tailscale-style IP.
- DLP false-positive measurements.
- TLS handling / cert pinning behavior.

## Adding a test

1. Pick the directory: `tests/unit/` for a pure unit test,
   `tests/integration/` for one that needs Docker.
2. Filename: `test_<topic>.py`.
3. Boilerplate:
   ```python
   import unittest

   from bot_bottle.<module> import <symbol>

   class TestThing(unittest.TestCase):
       def test_x(self):
           ...

   if __name__ == "__main__":
       unittest.main()
   ```
4. For Docker-dependent tests, decorate the class with
   `@skip_unless_docker()` from `tests._docker`.
