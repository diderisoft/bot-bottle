# Tests

Plain-Python test suite using stdlib `unittest`. No external
dependencies. Unit tests run anywhere Python 3 is present; integration
tests run through the backend named by `BOT_BOTTLE_BACKEND` (default
`docker`) and skip cleanly when that backend isn't available on the host.

## Layout

```
tests/
  fixtures.py                       # JSON manifest builders (shared)
  _backend.py                       # backend selection + skip guards (shared)
  unit/
    test_egress.py
    test_egress_addon_core.py
    test_manifest_egress.py
    test_dlp_detectors.py
    test_manifest_runtime.py
    ...                             # many others; see unit/ directory
  integration/
    test_gateway_image.py
    test_sandbox_escape.py
    ...
  canaries/
    test_gitleaks_release.py        # opt-in upstream artifact check
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

- `test_gateway_image.py` — builds Dockerfile.gateway and
  probes that gitleaks / mitmdump / supervise are all reachable
  inside the gateway image.
- `test_orchestrator_docker_auth.py` — drives the real control-plane
  container and verifies role-scoped authentication.
- `test_multitenant_isolation.py` and `test_sandbox_escape.py` — exercise
  token/allowlist separation and end-to-end escape attempts.

## Canaries

`tests/canaries/` holds upstream-regression checks gated on
`BOT_BOTTLE_RUN_CANARIES=1` and not part of the per-push suite.
They're invoked by the scheduled `canaries` workflow. The gitleaks canary
downloads the exact release archive pinned by `Dockerfile.gateway`, verifies
its architecture-specific checksum, and executes the binary.

```bash
BOT_BOTTLE_RUN_CANARIES=1 python -m scripts.unittest_gate \
  -t . -s tests/canaries -v --minimum-executed 1 --fail-on-skip
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
4. Skip guards live in `tests._backend` and gate on the backend's own
   readiness check, `bot_bottle.backend.has_backend` — the same probe
   behind `bot-bottle backend status`:
   - Backend-agnostic tests (go through `get_bottle_backend()`) decorate
     the class with `@skip_unless_selected_backend_available()` — the test
     runs against whichever backend `BOT_BOTTLE_BACKEND` selects and skips
     unless that backend is available (checking, e.g., Linux + `/dev/kvm`
     for Firecracker rather than unrelated Docker availability).
   - Backend-specific tests (exercise `DockerGateway`,
     `backend.docker.*`, …) decorate with `@skip_unless_backend("docker")`
     so they no-op under a run targeting a different backend.

   Each CI integration job runs `bot-bottle backend status --backend=<name>`
   as a preflight, which prints a clear per-check summary and exits non-zero
   when the backend is missing — so absent infrastructure fails the job
   instead of hiding among per-test `unittest.skip` lines.
