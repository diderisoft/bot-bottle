"""Publish the Firecracker guest kernel and dropbear to GHCR, for the
`publish-fc-guest` workflow:

    python -m bot_bottle.publish_fc_guest <arch> <vmlinux> <dropbear>

BOT_BOTTLE_INFRA_ARTIFACT_USER / _TOKEN are the registry's credentials, as for
`publish_images`. Prints each file's digest, to pin in `guest_files.DIGESTS`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .backend.firecracker.guest_files import DROPBEAR, KERNEL, REGISTRY, publish
from .oci_registry import Registry


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        sys.stderr.write("usage: python -m bot_bottle.publish_fc_guest "
                         "<arch> <vmlinux> <dropbear>\n")
        return 2
    arch, kernel, dropbear = args
    registry = Registry(REGISTRY, user=os.environ.get("BOT_BOTTLE_INFRA_ARTIFACT_USER", ""),
                        token=os.environ.get("BOT_BOTTLE_INFRA_ARTIFACT_TOKEN", ""))
    pinned = publish(registry, arch, {KERNEL: Path(kernel), DROPBEAR: Path(dropbear)})
    for name, digest in sorted(pinned.items()):
        print(f"{arch} {name} {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
