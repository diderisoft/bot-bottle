# Tests

Plain-Python test suite using stdlib `unittest`. No external
dependencies. Unit tests run anywhere Python 3 is present; integration
tests need Docker and skip cleanly otherwise.

## Layout

```
tests/
  run_tests.py                    # entry point
  fixtures.py                     # JSON manifest builders
  _docker.py                      # docker-availability skip helper
  test_pipelock_naming.py         # unit
  test_pipelock_classify.py       # unit
  test_pipelock_allowlist.py      # unit
  test_pipelock_yaml.py           # unit
  test_pipelock_image.py          # integration
  test_pipelock_sidecar_smoke.py  # integration
  test_dry_run_plan.py            # integration
  test_orphan_cleanup.py          # integration
```

## Running

```bash
tests/run_tests.py                                  # everything
tests/run_tests.py unit                             # unit only
tests/run_tests.py integration                      # integration only
tests/run_tests.py tests/test_pipelock_yaml.py      # one file
```

You can also run via `python -m unittest`:

```bash
python -m unittest discover -s tests
python -m unittest tests.test_pipelock_yaml
```

## What the integration tests cover

- `test_pipelock_image.py` — the pinned digest is reachable, ENTRYPOINT
  is `/pipelock`, and `CMD` includes `run`.
- `test_pipelock_sidecar_smoke.py` — `docker create` + `docker cp` the
  generated YAML to `/etc/pipelock.yaml` + `docker start`, then probe
  `/health`.
- `test_dry_run_plan.py` — `cli.py start --dry-run` shows the resolved
  egress allowlist and creates zero docker resources.
- `test_orphan_cleanup.py` — network_remove and pipelock_stop are
  idempotent against missing resources, so the EXIT trap can call them
  unconditionally.

## What's NOT covered

- `claude_bottle/ssh.py` end-to-end (would need a fake SSH host inside
  the container).
- A live SSH-through-pipelock tunnel against a real Tailscale-style IP.
- DLP false-positive measurements.
- TLS handling / cert pinning behavior.

## Adding a test

1. Pick a filename: `test_<topic>.py`. Add it to `INTEGRATION_NAMES`
   in `run_tests.py` if it needs Docker.
2. Boilerplate:
   ```python
   import unittest

   from claude_bottle.<module> import <symbol>

   class TestThing(unittest.TestCase):
       def test_x(self):
           ...

   if __name__ == "__main__":
       unittest.main()
   ```
3. For Docker-dependent tests, decorate the class with
   `@skip_unless_docker()` from `tests._docker`.
