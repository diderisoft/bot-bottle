"""Rotate the shared gateway's mitmproxy CA (issue #450).

    python -m bot_bottle.orchestrator.rotate_ca

A deliberate CA rollover has two halves: drop the *persisted* CA so a fresh one
is minted, and drop the *running* gateway so its mitmproxy (which holds the old
CA in memory) is replaced. This one-shot command does both:

  1. Delete the persisted CA under the host gateway-CA dir — the next gateway
     start generates a new one (mitmproxy reuses an existing CA, generates only
     when absent).
  2. Force-remove the infra / standalone-gateway containers so the stale
     in-memory CA is gone; the next bottle launch's idempotent `ensure_running`
     brings the gateway back up and mints the fresh CA.

It does NOT re-provision the new CA into already-running bottles — those must be
re-attached so they install the new trust anchor. Rotation is thus an explicit,
operator-driven action with a brief egress interruption, not an automatic one.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..docker_cmd import run_docker
from ..paths import host_gateway_ca_dir
from ..gateway import GATEWAY_NAME, rotate_gateway_ca
from .lifecycle import INFRA_NAME

# The containers whose mitmproxy would still be serving the old CA from memory:
# the consolidated infra container and the standalone per-host gateway.
_GATEWAY_CONTAINERS = (INFRA_NAME, GATEWAY_NAME)


def _out(msg: str) -> None:
    sys.stdout.write(f"rotate-ca: {msg}\n")


def main(argv: list[str] | None = None) -> int:
    del argv  # no flags — a single deliberate action
    ca_dir: Path = host_gateway_ca_dir()
    removed = rotate_gateway_ca(ca_dir)
    if removed:
        _out(f"removed {len(removed)} CA file(s) from {ca_dir}")
    else:
        _out(f"no persisted CA under {ca_dir}; a fresh one is minted on next start")

    # Drop any running gateway so its in-memory (now-stale) CA is replaced on
    # the next launch. `rm --force` on an absent name is a tolerated no-op.
    for name in _GATEWAY_CONTAINERS:
        proc = run_docker(["docker", "rm", "--force", name])
        if proc.returncode == 0 and proc.stdout.strip():
            _out(f"removed running container {name}")

    _out("done — the next bottle launch remints the CA; re-attach bottles to "
         "install the new trust anchor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
