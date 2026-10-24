"""`SupervisePlan`: the launch-time supervise plan, built by
`resolve_common.prepare_supervise`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SupervisePlan:
    """Output of `prepare_supervise`."""

    slug: str
