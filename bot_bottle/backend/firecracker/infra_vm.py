"""The per-host infra VM for the Firecracker backend (PRD 0070 Stage B).

A single persistent microVM that runs the orchestrator **control plane** (and,
in a following step, the gateway **data plane**) — the trusted per-host service
the docker backend runs as containers. It boots on the NAT'd orchestrator link
(`netpool.orch_slot()`): the host CLI reaches its control plane over HTTP at the
guest IP, and agent VMs reach its gateway ports over VM-to-VM routing.

Build-from-source (the default while the design churns): the rootfs is exported
from the locally built orchestrator image, which bakes the stdlib-only
control-plane source. A pull-from-registry mode (Gitea's OCI registry) becomes
the default later.

SSH is left enabled for debugging; the control plane is the load-bearing
surface.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ...log import die, info
from . import firecracker_vm, netpool, util

_ORCHESTRATOR_IMAGE = "bot-bottle-orchestrator:latest"
CONTROL_PLANE_PORT = 8099

# The infra VM makes direct upstream connections (gateway egress, and buildah
# during builds), and the kernel `ip=` cmdline sets no resolver. Public for
# now; routing DNS through a filtered path is a later refinement.
_INFRA_RESOLVER = "1.1.1.1"

_HEALTH_TIMEOUT_SECONDS = 45.0
_HEALTH_POLL_SECONDS = 0.5


@dataclass
class InfraVm:
    """A booted infra VM: its VMM handle, guest IP, and debug SSH key."""

    vm: firecracker_vm.VmHandle
    guest_ip: str
    private_key: Path

    @property
    def control_plane_url(self) -> str:
        return f"http://{self.guest_ip}:{CONTROL_PLANE_PORT}"

    def terminate(self) -> None:
        self.vm.terminate()


def build_infra_rootfs_dir() -> Path:
    """The infra VM's base rootfs: the orchestrator image (with its baked,
    stdlib-only source) prepared with the control-plane init as PID 1."""
    return util.build_base_rootfs_dir(
        _ORCHESTRATOR_IMAGE, variant="-infra", init_script=_infra_init(),
    )


def boot() -> InfraVm:
    """Boot the infra VM on the orchestrator link. Caller waits for health
    (`wait_for_health`) and tears it down."""
    slot = netpool.orch_slot()
    if not netpool.tap_present(slot.iface):
        die(f"orchestrator link {slot.iface} not present.\n"
            f"    ./cli.py backend setup --backend=firecracker")

    base = build_infra_rootfs_dir()
    run_dir = util.cache_dir() / "run" / "infra"
    run_dir.mkdir(parents=True, exist_ok=True)
    rootfs = run_dir / "rootfs.ext4"
    util.build_rootfs_ext4(base, rootfs, slack_mib=8192)
    private_key, pubkey = util.generate_keypair(run_dir)

    info(f"booting infra VM on {slot.iface} (guest {slot.guest_ip})")
    vm = firecracker_vm.boot(
        name="bot-bottle-infra", rootfs=rootfs, tap=slot.iface,
        guest_ip=slot.guest_ip, host_ip=slot.host_ip, pubkey=pubkey,
        run_dir=run_dir, mem_mib=4096,
    )
    return InfraVm(vm=vm, guest_ip=slot.guest_ip, private_key=private_key)


def wait_for_health(
    infra: InfraVm, *, timeout: float = _HEALTH_TIMEOUT_SECONDS,
) -> None:
    """Poll the control plane's /health until it answers 200 or the deadline
    passes. Dies (with the console tail) if the VMM exits early."""
    url = f"{infra.control_plane_url}/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not infra.vm.is_alive():
            die(f"infra VM exited during boot (rc={infra.vm.process.returncode}).\n"
                f"{firecracker_vm._console_tail(infra.vm.console_log)}")
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    info(f"infra control plane healthy at {infra.control_plane_url}")
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(_HEALTH_POLL_SECONDS)
    die(f"infra control plane at {url} did not become healthy within "
        f"{timeout:.0f}s.\n{firecracker_vm._console_tail(infra.vm.console_log)}")


def _infra_init() -> str:
    """PID-1 init for the infra VM: mount the pseudo-filesystems, wire a
    resolver, start dropbear (debug SSH), then launch the control plane. The
    gateway daemons are added here in the next step."""
    return f"""#!/bin/sh
# bot-bottle Firecracker infra VM init (PID 1).
mount -t proc proc /proc 2>/dev/null
mount -t sysfs sys /sys 2>/dev/null
mount -t devtmpfs dev /dev 2>/dev/null
mkdir -p /dev/pts && mount -t devpts devpts /dev/pts 2>/dev/null
mount -o remount,rw / 2>/dev/null

# Direct upstream resolver (control-plane / gateway egress + buildah).
printf 'nameserver {_INFRA_RESOLVER}\\n' > /etc/resolv.conf 2>/dev/null

# Debug SSH: install the per-boot pubkey from the kernel cmdline.
KEY=$(sed -n 's/.*bb_pubkey=\\([^ ]*\\).*/\\1/p' /proc/cmdline | base64 -d 2>/dev/null)
if [ -n "$KEY" ]; then
    mkdir -p /root/.ssh
    printf '%s\\n' "$KEY" > /root/.ssh/authorized_keys
    chmod 700 /root/.ssh && chmod 600 /root/.ssh/authorized_keys
fi
chown -R 0:0 /root 2>/dev/null || true
mkdir -p /etc/dropbear /run /var/lib/bot-bottle
/bb-dropbear -R -E -p 22 &

# Control plane. Source is baked at /app; the package is stdlib-only.
cd /app
BOT_BOTTLE_ROOT=/var/lib/bot-bottle python3 -m bot_bottle.orchestrator \\
    --host 0.0.0.0 --port {CONTROL_PLANE_PORT} --broker stub &

# Reap as PID 1; children are backgrounded, so `wait` blocks.
while : ; do wait ; done
"""
