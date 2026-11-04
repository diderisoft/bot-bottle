"""A bottle's `resources:` block: the memory and vCPUs its agent is given.

    resources:
      memory: 4G       # MiB as a number (4096), or "<n>M"/"<n>MiB"/"<n>G"/"<n>GiB"
      cpus: 4          # a whole number of vCPUs

Both keys are optional. Unset, a bottle gets the backend's usual size
(`backend.bottle_size`). Through `extends` and stacking, each key is
replaced by the later bottle that declares it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .util import ManifestError, as_json_object

MEMORY_MIN_MIB = 512
MEMORY_MAX_MIB = 64 * 1024
CPUS_MAX = 32

_KEYS = frozenset({"memory", "cpus"})
_MEMORY_RX = re.compile(r"^\s*(\d+)\s*(m|mib|g|gib)\s*$", re.IGNORECASE)


def parse_memory_mib(where: str, raw: object) -> int:
    """`raw` (a number of MiB, or a string with an M/MiB/G/GiB unit) as MiB."""
    if isinstance(raw, bool):
        raise ManifestError(f"{where} memory must be MiB or a size like \"4G\" (was a boolean)")
    if isinstance(raw, int):
        mib = raw
    elif isinstance(raw, str) and (m := _MEMORY_RX.match(raw)):
        mib = int(m.group(1)) * (1024 if m.group(2).lower().startswith("g") else 1)
    else:
        raise ManifestError(
            f"{where} memory must be MiB (e.g. 4096) or a size like \"4G\" or \"4096M\" "
            f"(was {raw!r})")
    if not MEMORY_MIN_MIB <= mib <= MEMORY_MAX_MIB:
        raise ManifestError(
            f"{where} memory must be between {MEMORY_MIN_MIB} MiB and "
            f"{MEMORY_MAX_MIB // 1024} GiB (was {mib} MiB)")
    return mib


def parse_cpus(where: str, raw: object) -> int:
    """`raw` as a whole number of vCPUs between 1 and `CPUS_MAX`."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ManifestError(f"{where} cpus must be a whole number (was {raw!r})")
    if not 1 <= raw <= CPUS_MAX:
        raise ManifestError(f"{where} cpus must be between 1 and {CPUS_MAX} (was {raw})")
    return raw


@dataclass(frozen=True)
class ManifestResources:
    """What a bottle declared; None is unset (the backend's usual size)."""
    memory_mib: int | None = None
    cpus: int | None = None

    @classmethod
    def from_dict(cls, name: str, raw: object) -> "ManifestResources":
        where = f"bottle '{name}' resources"
        d = as_json_object(raw, where)
        unknown = set(d) - _KEYS
        if unknown:
            raise ManifestError(
                f"{where} has unknown key(s) {sorted(unknown)}; allowed keys are cpus, memory.")
        return cls(
            memory_mib=parse_memory_mib(where, d["memory"]) if "memory" in d else None,
            cpus=parse_cpus(where, d["cpus"]) if "cpus" in d else None,
        )

    def overlay(self, later: "ManifestResources") -> "ManifestResources":
        """These resources with each key `later` declares replacing ours."""
        return ManifestResources(
            memory_mib=later.memory_mib if later.memory_mib is not None else self.memory_mib,
            cpus=later.cpus if later.cpus is not None else self.cpus,
        )

    def document(self) -> dict[str, object]:
        """The declared keys as a bottle file writes them (memory in MiB)."""
        out: dict[str, object] = {}
        if self.memory_mib is not None:
            out["memory"] = self.memory_mib
        if self.cpus is not None:
            out["cpus"] = self.cpus
        return out


__all__ = ["CPUS_MAX", "MEMORY_MAX_MIB", "MEMORY_MIN_MIB", "ManifestResources",
           "parse_cpus", "parse_memory_mib"]
