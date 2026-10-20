"""The Firecracker gateway data plane as a microVM (PRD 0070).

`FirecrackerGateway` is the Firecracker implementation of the backend-neutral
`Gateway` service, and owns the gateway's host-side logic directly: booting the
gateway microVM on its NAT'd link, seeding the pre-minted `gateway` token,
fetching the mitmproxy CA, and the SSH exec/cp provisioning transport. The
gateway VM never opens `bot-bottle.db` (#469), so it holds no signing key —
only the token the host hands it via `connect_to_orchestrator`.

The plane-agnostic VM substrate the orchestrator VM also uses stays in
`infra_vm` — booting a VM from a per-plane rootfs (`boot_vm`), the stable SSH
keypair, the secret-push retry loop, the PID lifecycle, and the
adoption/version helpers. The guest-side gateway daemon startup lives in the
gateway rootfs's guest init (`_gateway_init` in `infra_vm`), so it can't live in
a host-side method either. The pair coordinator (orchestrator-first, under a
singleton flock) is `FirecrackerInfraService` (`infra.py`).
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
from .. import util as backend_util
from . import infra_vm, netpool, util
from .gateway_transport import FirecrackerGatewayTransport

# The gateway microVM's name (its run dir). Fixed per host — one gateway VM,
# shared by every agent VM.
GATEWAY_NAME = "bot-bottle-gateway"

# The gateway VM's slim memory ceiling — the data plane carries no build
# tooling (buildah lives only on the orchestrator rootfs) — PRD 0070
# "Memory: fixed ceilings".
_GW_MEM_MIB = 2048

# The pre-minted `gateway` JWT path in the guest. The gateway init (in
# `infra_vm`) waits for it before starting the data plane, so its canonical
# definition lives with that init; imported here for the push so the
# load-bearing path isn't duplicated.
_GUEST_GATEWAY_JWT_PATH = infra_vm._GUEST_GATEWAY_JWT_PATH
# mitmproxy writes its CA here a beat after start; agents install it to trust
# the gateway's TLS interception. Host-side only (SSH cat), so it lives here.
_GATEWAY_CA_PATH = f"{MITMPROXY_HOME}/mitmproxy-ca-cert.pem"
# mitmproxy's signing identity (cert + key); the CA is persisted iff it is.
_CA_KEY_FILE = "mitmproxy-ca.pem"

_CA_FETCH_TIMEOUT_SECONDS = 15.0


class FirecrackerGateway(Gateway):
    """The consolidated gateway as a Firecracker microVM on the gateway link.

    The gateway rootfs is built/downloaded by `infra_vm.ensure_built` (the ABC's
    `ensure_built` no-op here); `connect_to_orchestrator` boots the VM resolving
    policy against the orchestrator and seeds the pre-minted `gateway` token."""

    name = GATEWAY_NAME

    def __init__(self, vm: infra_vm.InfraVm | None = None) -> None:
        # The live VM handle when this process booted it; None when adapting an
        # already-running gateway (CA fetch / provisioning go through the stable
        # key + fixed link, so no live handle is needed).
        self._vm = vm
        self._orchestrator_url = ""
        self._gateway_token = ""

    def connect_to_orchestrator(self, orchestrator_url: str, gateway_token: str) -> bool:
        """Boot the gateway VM on its link resolving policy against
        `orchestrator_url`, then seed `gateway_token` over SSH. The orchestrator's
        guest IP is parsed from the URL and passed on the cmdline (`bb_orch=`), so
        the baked init stays IP-independent. Boot the orchestrator first — the
        gateway daemons reach it at startup. The gateway never mints, so it holds
        no signing key, only this token (#469). Always a fresh boot (adoption
        happens a level up, in `FirecrackerInfraService`), so returns True."""
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
        # Boot on the gateway link from the gateway rootfs, restore the
        # persisted CA, then push the token the init waits for before starting
        # the data plane (so mitmproxy finds the CA rather than minting one).
        vm = infra_vm.boot_vm(
            name=GATEWAY_NAME, slot=netpool.gw_slot(), run_dir=infra_vm._gw_dir(),
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
        """Push the persisted CA into the freshly booted VM's mitmproxy
        confdir. The rootfs is rebuilt every boot, so without this each boot
        minted a new CA, and running agents (Node reads its CA file once, at
        start) kept trusting the old one. False when nothing is persisted
        yet."""
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
        """Save the CA mitmproxy just minted on first boot, so every later
        boot reuses it."""
        self.ca_cert_pem()  # waits until mitmproxy has written it
        self._save_ca(vm.private_key, vm.guest_ip)

    def _save_rolled_over_ca(self) -> None:
        # The VM's rootfs is rebuilt every boot: the rolled-over CA only
        # survives the restart if it's in the host dir `_restore_ca` pushes.
        self._save_ca(infra_vm._infra_dir() / "id_ed25519", netpool.gw_slot().guest_ip)

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
        """Stop only the gateway VM (idempotent — absent is success). A dead
        gateway already fails `infra_vm.adoptable`, so no version marker to
        clear here."""
        infra_vm._kill_pidfile(infra_vm._gw_dir())
        infra_vm._pid_file(infra_vm._gw_dir()).unlink(missing_ok=True)

    def address(self) -> str:
        """The gateway VM's guest IP — the agent-facing target agent VMs'
        gateway-port traffic is DNAT'd to."""
        return netpool.gw_slot().guest_ip

    def ca_cert_pem(self, *, timeout: float = DEFAULT_CA_TIMEOUT_SECONDS) -> str:
        """The gateway's mitmproxy CA (PEM) agents install to trust its TLS
        interception. Fetched from the gateway VM over SSH; polls because
        mitmproxy writes it a beat after boot. Works from any later launcher —
        falls back to the stable key + fixed link when this process didn't boot
        the VM."""
        key = self._vm.private_key if self._vm else infra_vm._infra_dir() / "id_ed25519"
        ip = self._vm.guest_ip if self._vm else netpool.gw_slot().guest_ip

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
        """The exec/cp transport git-gate provisioning stages per-bottle repos +
        deploy keys through (over SSH to the gateway VM). Needs no live handle —
        built from the stable key + the gateway link's guest IP, so teardown can
        use it too."""
        return FirecrackerGatewayTransport(
            infra_vm._infra_dir() / "id_ed25519", netpool.gw_slot().guest_ip)


__all__ = ["FirecrackerGateway", "FirecrackerGatewayTransport", "GATEWAY_NAME"]
