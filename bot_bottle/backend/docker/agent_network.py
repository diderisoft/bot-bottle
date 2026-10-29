"""Per-agent docker networks: each bottle's own `--internal` /29, shared only
with the gateway, so agents can't reach each other (#810).

A shared network with `enable_icc=false` doesn't work: ICC off drops all
container-to-container traffic on the bridge, the gateway's included.

The launcher creates the network before registering, so `docker network
create` is what allocates the subnet: a subnet a concurrent launch took fails
with an overlap and the next one is tried. Teardown disconnects the gateway
first, since docker refuses to remove a network with endpoints.
"""

from __future__ import annotations

from .gateway_net import NoFreeAddressError, free_agent_subnets
from .util import run_docker
from ...lib.log import warn

AGENT_NETWORK_PREFIX = "bot-bottle-agent-"
AGENT_NETWORK_LABEL = "bot-bottle.agent-network"
# Creation retries this many free-looking subnets before giving up.
_CREATE_ATTEMPTS = 16


class AgentNetworkError(RuntimeError):
    """A per-agent network couldn't be created, joined or read."""


def agent_network_name(slug: str) -> str:
    """The bottle's own network. Its prefix is under cleanup's `bot-bottle-`."""
    return f"{AGENT_NETWORK_PREFIX}{slug}"


def is_agent_network(name: str) -> bool:
    return name.startswith(AGENT_NETWORK_PREFIX)


def _docker_subnets() -> list[str]:
    """Every subnet of every docker network, so allocation skips them."""
    ids = run_docker(["docker", "network", "ls", "--quiet"])
    if ids.returncode != 0:
        raise AgentNetworkError(f"could not list docker networks: {ids.stderr.strip()}")
    if not ids.stdout.split():
        return []
    proc = run_docker([
        "docker", "network", "inspect",
        "--format", "{{range .IPAM.Config}}{{.Subnet}} {{end}}",
        *ids.stdout.split(),
    ])
    if proc.returncode != 0:
        raise AgentNetworkError(f"could not inspect docker networks: {proc.stderr.strip()}")
    return proc.stdout.split()


def network_subnet(name: str) -> str:
    """The network's IPv4 subnet, if it's an `--internal` per-agent network;
    raises otherwise."""
    proc = run_docker([
        "docker", "network", "inspect", "--format",
        f'{{{{index .Labels "{AGENT_NETWORK_LABEL}"}}}}|{{{{.Internal}}}}|'
        "{{range .IPAM.Config}}{{.Subnet}}{{end}}",
        name,
    ])
    if proc.returncode != 0:
        raise AgentNetworkError(f"could not inspect network {name}: {proc.stderr.strip()}")
    label, internal, subnet = (proc.stdout.strip().split("|") + ["", ""])[:3]
    if label != "1" or internal != "true" or not subnet:
        raise AgentNetworkError(
            f"network {name} is not an `--internal` bot-bottle agent network "
            f"(label {label or 'missing'}, internal {internal or 'unknown'}); "
            f"remove it (`docker network rm {name}`) and launch again")
    return subnet


def create_agent_network(name: str) -> str:
    """Create the bottle's `--internal` network on a free /29 and return the
    subnet. An existing per-agent network of that name (a crashed launch's) is
    reused."""
    tried = 0
    for subnet in free_agent_subnets(_docker_subnets()):
        proc = run_docker([
            "docker", "network", "create",
            "--internal",
            "--ipv6=false",
            "--subnet", subnet,
            "--label", f"{AGENT_NETWORK_LABEL}=1",
            name,
        ])
        if proc.returncode == 0:
            return subnet
        err = proc.stderr.lower()
        if "already exists" in err:
            return network_subnet(name)
        if "overlap" not in err:
            raise AgentNetworkError(f"agent network {name} failed to create: {proc.stderr.strip()}")
        # A concurrent launch took it: try the next.
        tried += 1
        if tried >= _CREATE_ATTEMPTS:
            break
    raise NoFreeAddressError(f"no free subnet for agent network {name}")


def connect_gateway(network: str, gateway: str, address: str) -> None:
    """Attach the gateway to the agent's network at `address`. Idempotent."""
    proc = run_docker(["docker", "network", "connect", "--ip", address, network, gateway])
    if proc.returncode != 0 and "already exists" not in proc.stderr:
        raise AgentNetworkError(
            f"gateway {gateway} failed to join agent network {network}: {proc.stderr.strip()}")


def remove_agent_network(name: str, gateway: str) -> None:
    """Detach the gateway and remove the network. Idempotent; warns rather than
    raises, since it runs in teardown. Only ever touches a per-agent network:
    the shared gateway network stays."""
    if not is_agent_network(name):
        return
    run_docker(["docker", "network", "disconnect", "--force", name, gateway])
    proc = run_docker(["docker", "network", "rm", name])
    if proc.returncode != 0 and "not found" not in proc.stderr.lower() \
            and "no such network" not in proc.stderr.lower():
        warn(f"could not remove agent network {name}: {proc.stderr.strip()}")


__all__ = [
    "AGENT_NETWORK_LABEL",
    "AGENT_NETWORK_PREFIX",
    "AgentNetworkError",
    "agent_network_name",
    "connect_gateway",
    "create_agent_network",
    "is_agent_network",
    "network_subnet",
    "remove_agent_network",
]
