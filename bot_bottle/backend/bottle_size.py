"""How much memory one bottle is given, the same on every backend: a
firecracker VM's memory, a macOS container's `--memory`, a Docker agent's
`mem_limit`. The host budget (`bot-bottle host budget --memory`) counts
running bottles at this size and refuses a launch that wouldn't fit.
"""

from __future__ import annotations

BOTTLE_MEMORY_MIB = 2048
BOTTLE_MEMORY_BYTES = BOTTLE_MEMORY_MIB * 1024 * 1024
