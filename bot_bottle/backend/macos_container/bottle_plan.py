"""Plan type for the macOS Apple Container backend."""

from __future__ import annotations

from dataclasses import dataclass

from .. import BottlePlan


@dataclass(frozen=True)
class MacosContainerBottlePlan(BottlePlan):
    # Guest-local container engine: the derived image, relaxed device mode and
    # the podman service.
    nested_containers: bool = False
