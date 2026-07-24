# Tests

Plain-Python test suite using stdlib `unittest`. No external
dependencies. Unit tests run anywhere Python 3 is present; integration
tests run through the backend named by `BOT_BOTTLE_BACKEND` (default
`docker`) and skip cleanly when that backend isn't available on the host.

## Layout

```
tests/
  fixtures.py                       # JSON manifest builders (shared)
  _backend.py                       # backend capability probes + skip guards
  backend_preflight.py              # `python -m tests.backend_preflight` (CI)
  unit/
    test_egress.py
    test_egress_addon_core.py
    test_manifest_egress.py
    test_dlp_detectors.py
    test_manifest_runtime.py
    ...                             # many others; see unit/ directory
  integration/
    test_gateway_image.py
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
- `test_gateway_image.py` — builds Dockerfile.gateway and
  probes that gitleaks / mitmdump / supervise are all reachable
  inside the gateway image.

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
   `tests/integration/` for one that needs a backend.
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
4. Skip guards live in `tests._backend`:
   - Backend-agnostic tests (go through `get_bottle_backend()`) decorate
     the class with `@skip_unless_selected_backend_available()` — the test
     runs against whichever backend `BOT_BOTTLE_BACKEND` selects and skips
     unless that backend's capability is present (a reachable Docker daemon,
     or an accessible `/dev/kvm` + `firecracker` for Firecracker).
   - Backend-specific tests (exercise `DockerBroker`, `DockerGateway`,
     `backend.docker.*`, …) decorate with `@skip_unless_backend("docker")`
     so they no-op under a run targeting a different backend.

   The same capability probes back the CI preflight,
   `python -m tests.backend_preflight [<backend>]`, which prints a clear
   PASS/FAIL line and exits non-zero when the selected backend is missing.
