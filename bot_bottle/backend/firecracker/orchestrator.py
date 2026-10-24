"""`FirecrackerOrchestrator`: the control plane as a microVM on its NAT'd link,
with the `/dev/vdb` registry volume. Seeds the signing key over SSH and waits
for `/health`. The CLI and the gateway both reach it at its guest IP. Shared VM
plumbing is in `infra_vm`.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from ...lib.log import die, info
from ...control.lifecycle import (
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    HostLink,
    Orchestrator,
)
from ...contract.hostctl import HOST_CONTROL, HOSTCTL_URL_ENV, Settings
from . import firecracker_vm, infra_vm, netpool, util
from .infra_vm import ORCHESTRATOR_PORT

# The orchestrator VM's run-dir name; one per host.
ORCHESTRATOR_NAME = "bot-bottle-orchestrator"

# Fixed at boot, demand-paged; room for in-VM buildah builds.
_ORCH_MEM_MIB = 4096

_HEALTH_POLL_SECONDS = 0.5
_SYNC_TIMEOUT_SECONDS = 15.0

# The registry volume: a host ext4 file attached as `/dev/vdb` at the DB dir,
# so the registry survives VM restarts. Loop-mount it (VM stopped) to inspect
# `bot-bottle.db`.
_REGISTRY_SIZE = "512M"

# The build-store volume (`/dev/vdc`): buildah's image store, so bases and
# layers survive restarts. Sparse; `image_builder` prunes it.
_BUILD_STORE_SIZE = "16G"


def registry_volume_path() -> Path:
    return infra_vm._infra_dir() / "registry.ext4"


def build_store_volume_path() -> Path:
    return infra_vm._infra_dir() / "build-store.ext4"


class FirecrackerOrchestrator(Orchestrator):
    """The control plane as a firecracker VM. Its rootfs comes from
    `infra_vm.ensure_built`; `ensure_running` boots, seeds the key and waits
    for `/health`."""

    name = ORCHESTRATOR_NAME

    def __init__(self, settings: Settings, vm: infra_vm.InfraVm | None = None) -> None:
        # The live VM handle if this process booted it; None when adopting one.
        self._settings = settings
        self._slot = netpool.from_settings(settings.infra).orch_slot()
        self._vm = vm

    def url(self) -> str:
        """The guest-IP URL the host CLI uses."""
        return f"http://{self._slot.guest_ip}:{ORCHESTRATOR_PORT}"

    def gateway_url(self) -> str:
        """Same as `url()`; the gateway gets it as `bb_orch=`."""
        return self.url()

    def host_link(self) -> HostLink:
        """The host end of the orchestrator's TAP, where the host controller
        accepts only the orchestrator VM."""
        return HostLink(address=self._slot.host_ip, network=f"{self._slot.guest_ip}/32",
                        backend="firecracker")

    def ssh_target(self) -> tuple[Path, str]:
        """(private key, guest IP) for SSH into the orchestrator VM, where
        agent builds run."""
        return infra_vm._infra_dir() / "id_ed25519", self._slot.guest_ip

    def is_running(self) -> bool:
        return infra_vm._pidfile_alive(infra_vm._orch_dir())

    def stop(self) -> None:
        """Stop only the orchestrator VM. Idempotent. Syncs the guest first,
        since the VM is killed and its volumes would lose unflushed writes."""
        if self.is_running():
            try:
                subprocess.run(
                    util.ssh_base_argv(*self.ssh_target()) + ["sync"],
                    capture_output=True, timeout=_SYNC_TIMEOUT_SECONDS, check=False,
                )
            except subprocess.TimeoutExpired:
                pass
        infra_vm._kill_pidfile(infra_vm._orch_dir())
        infra_vm._pid_file(infra_vm._orch_dir()).unlink(missing_ok=True)

    def ensure_running(
        self, *, startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    ) -> bool:
        """Boot the orchestrator VM with its registry volume, seed the key and
        wait for `/health`. A live, healthy one is left alone. Returns True if
        it booted, False if it adopted."""
        if self.is_running() and self.is_healthy():
            return False
        vm = infra_vm.boot_vm(
            settings=self._settings, name=ORCHESTRATOR_NAME, slot=self._slot,
            run_dir=infra_vm._orch_dir(), role="orchestrator", mem_mib=_ORCH_MEM_MIB,
            data_drives=(self._ensure_registry_volume(),
                         self._ensure_build_store_volume()),
        )
        # Push the host signing key the init waits for. Raises rather than hand
        # over an empty key.
        infra_vm.push_secret(
            vm, self.control_plane_key(), infra_vm._GUEST_SIGNING_KEY_PATH,
            "the control-plane signing key to the orchestrator VM "
            "(its control plane will not start)",
        )
        # The host controller's URL and token (empty when it isn't the one
        # starting this). The init waits for the file.
        host_env = self.host_control_env()
        lines = (host_env.get(HOSTCTL_URL_ENV, ""), host_env.get(HOST_CONTROL.token_env, ""))
        infra_vm.push_secret(
            vm, "".join(f"{line}\n" for line in lines), infra_vm.GUEST_HOSTCTL_PATH,
            "the host controller's address to the orchestrator VM",
        )
        self._vm = vm
        self._wait_for_health(vm, timeout=startup_timeout)
        return True

    def _wait_for_health(
        self, vm: infra_vm.InfraVm, *, timeout: float,
    ) -> None:
        """Poll `/health` until 200 or the deadline. Dies with the console tail
        if the VMM exits."""
        url = f"{self.url()}/health"
        deadline = time.monotonic() + timeout
        problem: str | None = "it was never probed"
        while time.monotonic() < deadline:
            if vm.vm is not None and not vm.vm.is_alive():
                die(f"orchestrator VM exited during boot (rc={vm.vm.process.returncode}).\n"
                    f"{firecracker_vm._console_tail(vm.vm.console_log)}")
            problem = self.health_problem()
            if problem is None:
                info(f"orchestrator control plane healthy at {self.url()}")
                return
            time.sleep(_HEALTH_POLL_SECONDS)
        tail = (firecracker_vm._console_tail(vm.vm.console_log)
                if vm.vm is not None else "")
        die(f"orchestrator control plane at {url} did not become healthy within "
            f"{timeout:.0f}s ({problem}).\n{tail}")

    def _ensure_registry_volume(self) -> Path:
        """Create the registry volume on first use."""
        return _ensure_volume(registry_volume_path(), _REGISTRY_SIZE, "registry")

    def _ensure_build_store_volume(self) -> Path:
        """Create the build-store volume on first use."""
        return _ensure_volume(build_store_volume_path(), _BUILD_STORE_SIZE, "build-store")


def _ensure_volume(vol: Path, size: str, label: str) -> Path:
    if vol.exists():
        return vol
    info(f"creating infra {label} volume {vol} ({size})")
    proc = subprocess.run(
        ["mke2fs", "-q", "-t", "ext4", "-F", str(vol), size],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        vol.unlink(missing_ok=True)
        die(f"creating {label} volume failed: {proc.stderr.strip()}")
    return vol


__all__ = [
    "FirecrackerOrchestrator",
    "ORCHESTRATOR_NAME",
    "build_store_volume_path",
    "registry_volume_path",
]
