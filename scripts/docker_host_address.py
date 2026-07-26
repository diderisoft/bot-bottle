#!/usr/bin/env python3
"""Print the Docker host address seen by a socket-shared Linux CI container."""

from __future__ import annotations

import socket
import struct
import sys
from pathlib import Path


def default_ipv4_gateway(route_table: str) -> str:
    """Return the active default gateway from Linux ``/proc/net/route``."""

    for line in route_table.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[1] != "00000000":
            continue
        try:
            gateway = int(fields[2], 16)
            flags = int(fields[3], 16)
        except ValueError:
            continue
        if flags & 0x2:  # RTF_GATEWAY
            return socket.inet_ntoa(struct.pack("<I", gateway))
    raise ValueError("no active IPv4 default gateway in /proc/net/route")


def main() -> int:
    try:
        print(default_ipv4_gateway(Path("/proc/net/route").read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        print(f"docker-host-address: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
