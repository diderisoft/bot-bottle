"""Docker network helpers. Names are `bot-bottle-net-<slug>` (internal) and
`bot-bottle-egress-<slug>`, suffixed `-2`, `-3`, ... (up to 100) on conflict.
User-defined bridges are used because only they run Docker's embedded DNS.
"""

from __future__ import annotations

import subprocess

from ...lib.log import die, info, warn


def network_name_for_slug(slug: str) -> str:
    return f"bot-bottle-net-{slug}"


def network_egress_name_for_slug(slug: str) -> str:
    return f"bot-bottle-egress-{slug}"


def network_exists(name: str) -> bool:
    """`network inspect`, since `network ls -f name=` matches substrings."""
    return (
        subprocess.run(
            ["docker", "network", "inspect", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def _network_create_with_prefix(base: str, internal: bool) -> str:
    """Create a network named `base`, suffixed on conflict (up to 100). Returns
    the name."""
    name = base
    suffix = 2
    while network_exists(name):
        name = f"{base}-{suffix}"
        suffix += 1
        if suffix > 100:
            die(
                f"could not find a free network name after {base}-99; "
                f"clean up old networks with 'docker network rm <name>'"
            )

    kind = "internal" if internal else "bridge (egress)"
    args = ["docker", "network", "create"]
    if internal:
        args.append("--internal")
    args.append(name)
    info(f"creating {kind} network {name}")
    result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        flag = " --internal" if internal else ""
        die(f"docker network create{flag} {name} failed")
    return name


def network_create_internal(slug: str) -> str:
    """Create an `--internal` network. Returns the name."""
    return _network_create_with_prefix(network_name_for_slug(slug), internal=True)


def network_create_egress(slug: str) -> str:
    """Create a user-defined bridge, which has DNS for upstream names."""
    return _network_create_with_prefix(network_egress_name_for_slug(slug), internal=False)


def network_inspect_cidr(name: str) -> str:
    """The IPv4 CIDR Docker assigned to a user-defined network."""
    result = subprocess.run(
        ["docker", "network", "inspect",
         "--format", "{{range .IPAM.Config}}{{.Subnet}}{{end}}", name],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        die(f"docker network inspect {name} failed: {result.stderr.strip()}")
    cidr = result.stdout.strip()
    if not cidr:
        die(f"network {name!r} has no IPAM subnet configured")
    return cidr


def network_attach(network: str, container: str) -> None:
    result = subprocess.run(
        ["docker", "network", "connect", network, container],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        die(f"docker network connect {network} {container} failed")


def network_remove(name: str) -> bool:
    """Remove a network. A missing one is success; returns True if it's gone."""
    if not network_exists(name):
        return True
    result = subprocess.run(
        ["docker", "network", "rm", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        warn(f"failed to remove network {name}; clean up with 'docker network rm {name}'")
        return False
    return True
