"""Rotate every backend's gateway mitmproxy CA (issue #450).

    python -m bot_bottle.orchestrator.rotate_ca

A deliberate CA rollover has two halves: drop the *persisted* CA so a fresh one
is minted, and drop the *running* gateway so its mitmproxy (which holds the old
CA in memory) is replaced. For each backend available on this host, this
one-shot command does both:

  1. Delete the gateway's persisted CA (`Gateway.persisted_ca_dir`): the next
     gateway start generates a new one (mitmproxy reuses an existing CA and
     generates one only when absent).
  2. Stop the gateway so the stale in-memory CA is gone. The next bring-up
     starts it again, minting the fresh CA, and reattaches every running bottle
     (PRD 0081), which installs the new CA in each.

Running bottles lose egress between the two: rotation is an explicit,
operator-driven action, not an automatic one.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from ..gateway import Gateway, rotate_gateway_ca


def _out(msg: str) -> None:
    sys.stdout.write(f"rotate-ca: {msg}\n")


def _gateways() -> list[tuple[str, Callable[[], Gateway]]]:
    """(backend name, gateway factory) for every registered backend available
    on this host. Driven by the backend registry and each backend's
    `infra_service()`, so a new backend is covered without changes here."""
    from ..backend import get_bottle_backend, has_backend, known_backend_names
    return [
        (name, lambda name=name: get_bottle_backend(name).infra_service().gateway())
        for name in known_backend_names()
        if has_backend(name)
    ]


def main(argv: list[str] | None = None) -> int:
    del argv  # no flags — a single deliberate action
    gateways = _gateways()
    if not gateways:
        _out("no backend available on this host; nothing to rotate")
        return 0
    for name, factory in gateways:
        gateway = factory()
        ca_dir = gateway.persisted_ca_dir()
        if ca_dir is None:
            _out(f"{name}: CA isn't in a host dir (named volume); remove it to rotate")
        else:
            removed = rotate_gateway_ca(ca_dir)
            _out(f"{name}: removed {len(removed)} CA file(s) from {ca_dir}" if removed
                 else f"{name}: no persisted CA under {ca_dir}")
        gateway.stop()
        _out(f"{name}: stopped the gateway")
    _out("done — the next bottle launch mints the new CA and reattaches running "
         "bottles to it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
