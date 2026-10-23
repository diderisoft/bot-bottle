"""Plan type for the macOS Apple Container backend."""

from __future__ import annotations

from dataclasses import dataclass

from .. import BottlePlan


@dataclass(frozen=True)
class MacosContainerBottlePlan(BottlePlan):
    # Guest-local container engine. Gates the derived image, the
    # device-mode relaxation, and the resident podman service.
    nested_containers: bool = False
