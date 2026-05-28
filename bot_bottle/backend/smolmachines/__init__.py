"""smolmachines bottle backend (PRD 0023).

Selectable via `BOT_BOTTLE_BACKEND=smolmachines`. Runs each
bottle inside a per-agent microVM (libkrun / Hypervisor.framework
on macOS) with a userspace gvproxy gateway as the egress
primitive. The sidecar bundle (PRD 0024) runs as a host-side
docker container reached only through gvproxy's port-forward list.

Chunk 1 (this commit) ships the backend skeleton + Smolfile +
gvproxy renderers + preflight check. VM lifecycle, sidecar
bringup, and provisioning land in later chunks."""

from .backend import SmolmachinesBottleBackend  # noqa: F401

__all__ = ["SmolmachinesBottleBackend"]
