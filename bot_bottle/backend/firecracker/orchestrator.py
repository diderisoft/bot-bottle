"""The Firecracker orchestrator (control plane) as a microVM (PRD 0070).

`FirecrackerOrchestrator` is the Firecracker implementation of the backend-neutral
`Orchestrator` service, and owns the control plane's host-side logic directly:
booting the orchestrator microVM on its NAT'd link with the persistent registry
volume (/dev/vdb — the sole opener of `bot-bottle.db`), seeding the host-canonical
signing key over SSH, and waiting for `/health`. The host CLI reaches it at its
guest IP, and so does the gateway (`bb_orch` cmdline), so `url()` == `gateway_url()`.

The plane-agnostic VM substrate the gateway VM also uses stays in `infra_vm` —
booting a VM from a per-plane rootfs (`boot_vm`), the stable SSH keypair, the
secret-push retry, the PID lifecycle, the adoption/version helpers, and the
per-plane guest inits (`role_init`). The pair coordinator (adopt-or-boot-both
under a singleton flock) is `FirecrackerInfraService` (`infra.py`).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from ...log import die, info
from ...paths import host_orchestrator_token
from ...orchestrator.lifecycle import (
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    Orchestrator,
)
from . import firecracker_vm, infra_vm, netpool
from .infra_vm import ORCHESTRATOR_PORT

# The orchestrator microVM's name (its run dir). Fixed per host — one
# control-plane VM.
ORCHESTRATOR_NAME = "bot-bottle-orchestrator"

# Memory ceiling (fixed at boot, demand-paged). The orchestrator keeps the build
# headroom (buildah's 2-4 GB working set during in-VM agent builds) — PRD 0070
# "Memory: fixed ceilings".
_ORCH_MEM_MIB = 4096

_HEALTH_POLL_SECONDS = 0.5

# The registry "volume": a host-side ext4 file attached to the orchestrator VM as
# a second virtio-block device (guest /dev/vdb), mounted at the control plane's DB
# dir. It outlives the ephemeral rootfs, so the bottle registry survives an
# orchestrator-VM restart — the firecracker analogue of a docker volume. A plain
# ext4 file: `sudo mount -o loop <path>` on the host (VM stopped) to inspect
# bot-bottle.db. The gateway VM has no such volume — the data plane never opens
# the DB (#469).
_REGISTRY_SIZE = "512M"


def registry_volume_path() -> Path:
    return infra_vm._infra_dir() / "registry.ext4"


class FirecrackerOrchestrator(Orchestrator):
    """The control plane as a Firecracker microVM on the orchestrator link.

    The orchestrator rootfs is built/downloaded by `infra_vm.ensure_built` (the
    ABC's `ensure_built` no-op here); `ensure_running` boots the VM, seeds the
    signing key, and blocks until `/health` answers."""

    name = ORCHESTRATOR_NAME

    def __init__(self, vm: infra_vm.InfraVm | None = None) -> None:
        # The live VM handle when this process booted it; None when adapting an
        # already-running orchestrator (health/URL go through the fixed link).
        self._vm = vm

    def url(self) -> str:
        """The orchestrator VM's guest IP URL — where the host CLI reaches the
        control plane."""
        return f"http://{netpool.orch_slot().guest_ip}:{ORCHESTRATOR_PORT}"

    def gateway_url(self) -> str:
        """Same guest IP the host uses — the gateway resolves the orchestrator at
        `bb_orch=<guest_ip>` off its cmdline."""
        return self.url()

    def ssh_target(self) -> tuple[Path, str]:
        """(private key, guest IP) for SSHing into the orchestrator VM. In-VM
        agent-image builds (buildah) run here — the build host drives them over
        SSH with the stable infra key."""
        return infra_vm._infra_dir() / "id_ed25519", netpool.orch_slot().guest_ip

    def is_running(self) -> bool:
        return infra_vm._pidfile_alive(infra_vm._orch_dir())

    def stop(self) -> None:
        """Stop only the orchestrator VM (idempotent). A dead orchestrator fails
        `infra_vm.adoptable`'s health check, so no version marker to clear."""
        infra_vm._kill_pidfile(infra_vm._orch_dir())
        infra_vm._pid_file(infra_vm._orch_dir()).unlink(missing_ok=True)

    def ensure_running(
        self, *, startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        """Boot the orchestrator VM on its link with the persistent registry
        volume, seed the signing key over SSH, and block until `/health` answers.
        Idempotent — a live, healthy control plane is left alone (the pair
        coordinator otherwise `stop()`s it before calling, so this boots fresh)."""
        if self.is_running() and self.is_healthy():
            return
        vm = infra_vm.boot_vm(
            name=ORCHESTRATOR_NAME, slot=netpool.orch_slot(),
            run_dir=infra_vm._orch_dir(), role="orchestrator", mem_mib=_ORCH_MEM_MIB,
            data_drive=self._ensure_registry_volume(),
        )
        # Push the host-canonical signing key (the init waits for it before
        # starting the control plane). The host token file stays the single
        # source of truth, so a co-running docker/macOS control plane keeps
        # working; the guest verifies tokens with the same key the CLI signs from.
        infra_vm.push_secret(
            vm, host_orchestrator_token(), infra_vm._GUEST_SIGNING_KEY_PATH,
            "the control-plane signing key to the orchestrator VM "
            "(its control plane will not start)",
        )
        self._vm = vm
        self._wait_for_health(vm, timeout=startup_timeout)

    def _wait_for_health(
        self, vm: infra_vm.InfraVm, *, timeout: float,
    ) -> None:
        """Poll `/health` until it answers 200 or the deadline passes. Dies (with
        the console tail) if the VMM exits early."""
        url = f"{self.url()}/health"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if vm.vm is not None and not vm.vm.is_alive():
                die(f"orchestrator VM exited during boot (rc={vm.vm.process.returncode}).\n"
                    f"{firecracker_vm._console_tail(vm.vm.console_log)}")
            if self.is_healthy():
                info(f"orchestrator control plane healthy at {self.url()}")
                return
            time.sleep(_HEALTH_POLL_SECONDS)
        tail = (firecracker_vm._console_tail(vm.vm.console_log)
                if vm.vm is not None else "")
        die(f"orchestrator control plane at {url} did not become healthy within "
            f"{timeout:.0f}s.\n{tail}")

    def _ensure_registry_volume(self) -> Path:
        """Create the empty ext4 registry volume on first use; reuse it after."""
        vol = registry_volume_path()
        if vol.exists():
            return vol
        info(f"creating infra registry volume {vol} ({_REGISTRY_SIZE})")
        proc = subprocess.run(
            ["mke2fs", "-q", "-t", "ext4", "-F", str(vol), _REGISTRY_SIZE],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            vol.unlink(missing_ok=True)
            die(f"creating registry volume failed: {proc.stderr.strip()}")
        return vol


__all__ = ["FirecrackerOrchestrator", "ORCHESTRATOR_NAME", "registry_volume_path"]
