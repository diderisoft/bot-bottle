"""`FirecrackerGateway`: the gateway as a microVM on its NAT'd link. Boots it,
seeds the `gateway` token, persists and restores the CA, and provides the SSH
provisioning transport. Shared VM plumbing is in `infra_vm`; the pair is
coordinated by `FirecrackerInfraService`.
"""

from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path
from urllib.parse import urlparse

from ...control.gateway import (
    DEFAULT_CA_TIMEOUT_SECONDS,
    GATEWAY_CA_GLOB,
    MITMPROXY_HOME,
    Gateway,
    GatewayError,
    GatewayTransport,
)
from ...contract.hostctl import Settings
from .. import util as backend_util
from . import infra_vm, netpool, util
from .gateway_transport import FirecrackerGatewayTransport

# The gateway VM's run-dir name; one per host.
GATEWAY_NAME = "bot-bottle-gateway"

# No build tooling on the gateway, so less memory.
_GW_MEM_MIB = 2048

# Defined with the gateway init in `infra_vm`, which waits for it.
_GUEST_GATEWAY_JWT_PATH = infra_vm._GUEST_GATEWAY_JWT_PATH
# Where mitmproxy writes its CA shortly after start.
_GATEWAY_CA_PATH = f"{MITMPROXY_HOME}/mitmproxy-ca-cert.pem"
# mitmproxy's cert+key; the CA is persisted iff this is.
_CA_KEY_FILE = "mitmproxy-ca.pem"

_CA_FETCH_TIMEOUT_SECONDS = 15.0


class FirecrackerGateway(Gateway):
    """The gateway VM. Its rootfs comes from `infra_vm.ensure_built`;
    `connect_to_orchestrator` boots it and seeds the token."""

    name = GATEWAY_NAME

    def __init__(self, settings: Settings, vm: infra_vm.InfraVm | None = None) -> None:
        # The live VM handle if this process booted it; None when adopting one.
        self._settings = settings
        self._slot = netpool.from_settings(settings.infra).gw_slot()
        self._vm = vm
        self._orchestrator_url = ""
        self._gateway_token = ""

    def connect_to_orchestrator(self, orchestrator_url: str, gateway_token: str) -> bool:
        """Boot the gateway VM pointed at `orchestrator_url` (its IP goes on
        the cmdline as `bb_orch=`) and seed `gateway_token` over SSH. The
        orchestrator must be up first. Always a fresh boot, so returns True."""
        self._orchestrator_url = orchestrator_url
        self._gateway_token = gateway_token
        if not self._orchestrator_url:
            raise GatewayError(
                "gateway requires an orchestrator URL to run "
                "(resolver-only data plane; no single-tenant fallback)"
            )
        if not self._gateway_token:
            raise GatewayError(
                "gateway requires a pre-minted `gateway` token to run "
                "(the orchestrator mints it; the gateway never holds the key)"
            )
        orchestrator_guest_ip = urlparse(self._orchestrator_url).hostname or ""
        if not orchestrator_guest_ip:
            raise GatewayError(
                f"cannot resolve orchestrator guest IP from {self._orchestrator_url!r}"
            )
        # Restore the persisted CA, then push the token the init waits for, so
        # mitmproxy finds the CA instead of minting one.
        vm = infra_vm.boot_vm(
            settings=self._settings,
            name=GATEWAY_NAME, slot=self._slot, run_dir=infra_vm._gw_dir(),
            role="gateway", mem_mib=_GW_MEM_MIB,
            extra_boot_args=f"bb_orch={orchestrator_guest_ip}",
        )
        restored = self._restore_ca(vm)
        infra_vm.push_secret(
            vm, self._gateway_token, _GUEST_GATEWAY_JWT_PATH,
            "the gateway JWT to the gateway VM (its data plane will not start)",
        )
        self._vm = vm
        if not restored:
            self._persist_ca(vm)
        return True

    def _restore_ca(self, vm: infra_vm.InfraVm) -> bool:
        """Push the persisted CA into the fresh VM, since the rootfs is rebuilt
        each boot. False when nothing is persisted yet."""
        files = sorted(infra_vm.gateway_ca_dir().glob(GATEWAY_CA_GLOB))
        if not any(f.name == _CA_KEY_FILE for f in files):
            return False
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for path in files:
                tar.add(path, arcname=path.name)
        infra_vm.push_stream(
            vm, buf.getvalue(),
            f"umask 077; mkdir -p {MITMPROXY_HOME} && tar -C {MITMPROXY_HOME} -xf -",
            "the persisted gateway CA to the gateway VM",
        )
        return True

    def _persist_ca(self, vm: infra_vm.InfraVm) -> None:
        """Save the CA mitmproxy minted on first boot."""
        self.ca_cert_pem()  # waits until mitmproxy has written it
        self._save_ca(vm.private_key, vm.guest_ip)

    def _save_rolled_over_ca(self) -> None:
        # Keep a rolled-over CA across reboots.
        self._save_ca(infra_vm._infra_dir() / "id_ed25519", self._slot.guest_ip)

    def _save_ca(self, key: Path, ip: str) -> None:
        """Copy the gateway VM's CA files to the persisted CA dir."""
        proc = subprocess.run(
            util.ssh_base_argv(key, ip)
            + [f"cd {MITMPROXY_HOME} && tar -cf - {GATEWAY_CA_GLOB}"],
            capture_output=True, timeout=_CA_FETCH_TIMEOUT_SECONDS, check=False,
        )
        if proc.returncode != 0:
            raise GatewayError(
                "could not save the gateway CA: "
                f"{proc.stderr.decode(errors='replace').strip() or '<no stderr>'}")
        dest = infra_vm.gateway_ca_dir()
        with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r") as tar:
            for member in tar.getmembers():
                name = Path(member.name).name
                source = tar.extractfile(member)
                if not member.isfile() or source is None or not name.startswith("mitmproxy-ca"):
                    continue
                target = dest / name
                target.write_bytes(source.read())
                target.chmod(0o600)

    def persisted_ca_dir(self) -> Path | None:
        return infra_vm.gateway_ca_dir()

    def is_running(self) -> bool:
        return infra_vm._pidfile_alive(infra_vm._gw_dir())

    def stop(self) -> None:
        """Stop the gateway VM. Idempotent."""
        infra_vm._kill_pidfile(infra_vm._gw_dir())
        infra_vm._pid_file(infra_vm._gw_dir()).unlink(missing_ok=True)

    def address(self) -> str:
        """The gateway VM's guest IP, which agent traffic is DNAT'd to."""
        return self._slot.guest_ip

    def ca_cert_pem(self, *, timeout: float = DEFAULT_CA_TIMEOUT_SECONDS) -> str:
        """The gateway's CA PEM, fetched over SSH and polled for. Works from
        any launcher via the stable key and fixed link."""
        key = self._vm.private_key if self._vm else infra_vm._infra_dir() / "id_ed25519"
        ip = self._vm.guest_ip if self._vm else self._slot.guest_ip

        def _fetch() -> str | None:
            proc = subprocess.run(
                util.ssh_base_argv(key, ip) + [f"cat {_GATEWAY_CA_PATH}"],
                capture_output=True, text=True,
                timeout=_CA_FETCH_TIMEOUT_SECONDS, check=False,
            )
            ok = proc.returncode == 0 and "BEGIN CERTIFICATE" in proc.stdout
            return proc.stdout if ok else None

        try:
            return backend_util.poll_ca_cert(_fetch, timeout=timeout)
        except TimeoutError as exc:
            raise GatewayError(str(exc)) from exc

    def provisioning_transport(self) -> GatewayTransport:
        """The SSH exec/cp transport for git-gate provisioning. Needs no live
        handle."""
        return FirecrackerGatewayTransport(
            infra_vm._infra_dir() / "id_ed25519", self._slot.guest_ip)


__all__ = ["FirecrackerGateway", "FirecrackerGatewayTransport", "GATEWAY_NAME"]
