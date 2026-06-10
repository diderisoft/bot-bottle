"""Launch flow for the macOS Apple Container backend.

The backend is registered and its host primitives are implemented, but
full launch is intentionally blocked until the sidecar network
enforcement design is finished. Apple Container can publish ports and
create networks, but bot-bottle's Docker topology relies on an agent
container attached only to an internal network while the sidecar bundle
also has egress. The first runnable version must preserve that
no-direct-egress property.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Generator

from ...log import die
from .bottle import MacosContainerBottle
from .bottle_plan import MacosContainerBottlePlan


@contextmanager
def launch(
    plan: MacosContainerBottlePlan,
    *,
    provision: Callable[[MacosContainerBottlePlan, "MacosContainerBottle"], str | None],
) -> Generator[MacosContainerBottle, None, None]:
    del provision
    die(
        "macos-container backend launch is not enabled yet: "
        "the backend primitives are present, but sidecar network "
        "enforcement still needs implementation."
    )
    yield  # pragma: no cover
