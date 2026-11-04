"""How much memory and how many vCPUs one bottle is given. Unless its
`resources:` block says otherwise, a bottle gets the same on every backend: a
firecracker VM's memory, a macOS container's `--memory`, a Docker agent's
`mem_limit`. The host budget (`bot-bottle host budget --memory`) counts
running bottles at their size and refuses a launch that wouldn't fit.
"""

from __future__ import annotations

from typing import Any, Mapping

BOTTLE_MEMORY_MIB = 2048
BOTTLE_MEMORY_BYTES = BOTTLE_MEMORY_MIB * 1024 * 1024
# A firecracker VM's vCPUs when the bottle sets none. Docker and macOS
# containers keep their runtime's default instead.
FIRECRACKER_DEFAULT_CPUS = 2


def memory_mib(bottle: Any) -> int:
    """The MiB `bottle` (a `ManifestBottle`) is given."""
    declared = getattr(getattr(bottle, "resources", None), "memory_mib", None)
    return declared if isinstance(declared, int) else BOTTLE_MEMORY_MIB


def memory_bytes(bottle: Any) -> int:
    return memory_mib(bottle) * 1024 * 1024


def cpus(bottle: Any) -> int | None:
    """The vCPUs `bottle` declared, or None for the backend's default."""
    declared = getattr(getattr(bottle, "resources", None), "cpus", None)
    return declared if isinstance(declared, int) else None


def recorded_memory(meta: Mapping[str, object]) -> int:
    """The bytes a running bottle was given, from its session record (older
    records, from before `resources:`, have the fixed size)."""
    value = meta.get("memory")
    return value if isinstance(value, int) and value > 0 else BOTTLE_MEMORY_BYTES
