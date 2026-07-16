"""Firecracker microVM process lifecycle.

A bottle VM is one `firecracker` process booted from a JSON config
(kernel + writable rootfs ext4 + one TAP network interface). We run it
`--no-api`: all configuration is in the config file, and teardown is a
process signal — the microVM dies with its VMM, which is exactly the
ephemeral-bottle semantics we want. No API socket, no runtime
reconfiguration.

The guest gets its IP from the kernel `ip=` cmdline (kernel-level
autoconfig, no in-guest iproute2) and its SSH pubkey from a `bb_pubkey=`
cmdline arg the init decodes.
"""

from __future__ import annotations

import base64
import json
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ...log import die, info
from . import util


# Serial console off the critical path but captured to a log for
# debugging boot failures. `reboot=k panic=1 pci=off` are the standard
# Firecracker guest args; `i8042.*` skip the (absent) PS/2 probe to
# shave boot time.
_BASE_BOOT_ARGS = (
    "console=ttyS0 reboot=k panic=1 pci=off "
    "i8042.noaux i8042.nomux i8042.nopnp i8042.dumbkbd "
    "root=/dev/vda rw init=/bb-init"
)


@dataclass
class VmHandle:
    """A running microVM: its VMM process, guest IP, and console log."""

    process: subprocess.Popen[bytes]
    guest_ip: str
    console_log: Path

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def terminate(self) -> None:
        """Stop the VMM (and thus the guest). SIGTERM, then SIGKILL."""
        if self.process.poll() is not None:
            return
        self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def _boot_args(guest_ip: str, host_ip: str, pubkey: str) -> str:
    # ip=<client>::<gw>:<netmask>::<dev>:off — /31 point-to-point link,
    # so netmask is 255.255.255.254 and the gateway is the host TAP IP.
    ip_arg = f"ip={guest_ip}::{host_ip}:255.255.255.254::eth0:off"
    pub_b64 = base64.b64encode(pubkey.encode()).decode()
    return f"{_BASE_BOOT_ARGS} {ip_arg} bb_pubkey={pub_b64}"


def _config(
    *,
    rootfs: Path,
    tap: str,
    guest_ip: str,
    host_ip: str,
    pubkey: str,
    vcpus: int,
    mem_mib: int,
    guest_mac: str,
    data_drive: Path | None = None,
) -> dict[str, object]:
    drives: list[dict[str, object]] = [
        {
            "drive_id": "rootfs",
            "path_on_host": str(rootfs),
            "is_root_device": True,
            "is_read_only": False,
        }
    ]
    # A second virtio-block device (guest /dev/vdb) — the infra VM's
    # persistent registry "volume", a host-side ext4 file that outlives the
    # ephemeral rootfs across VM restarts.
    if data_drive is not None:
        drives.append({
            "drive_id": "data",
            "path_on_host": str(data_drive),
            "is_root_device": False,
            "is_read_only": False,
        })
    return {
        "boot-source": {
            "kernel_image_path": str(util.kernel_path()),
            "boot_args": _boot_args(guest_ip, host_ip, pubkey),
        },
        "drives": drives,
        "network-interfaces": [
            {
                "iface_id": "eth0",
                "host_dev_name": tap,
                "guest_mac": guest_mac,
            }
        ],
        "machine-config": {
            "vcpu_count": vcpus,
            "mem_size_mib": mem_mib,
        },
    }


def boot(
    *,
    name: str,
    rootfs: Path,
    tap: str,
    guest_ip: str,
    host_ip: str,
    pubkey: str,
    run_dir: Path,
    vcpus: int = 2,
    mem_mib: int = 2048,
    guest_mac: str = "06:00:AC:10:00:02",
    detached: bool = False,
    data_drive: Path | None = None,
) -> VmHandle:
    """Write the config and launch the VMM. Returns once the process is
    spawned; callers wait for SSH readiness separately.

    `detached` starts the VMM in its own session (`start_new_session`) so it
    survives the launcher exiting — used for the persistent per-host infra
    VM, which must outlive the short-lived `start` process (agent VMs stay
    attached and are torn down with the launcher)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    config_path = run_dir / "config.json"
    console_log = run_dir / "console.log"
    config_path.write_text(json.dumps(
        _config(
            rootfs=rootfs, tap=tap, guest_ip=guest_ip, host_ip=host_ip,
            pubkey=pubkey, vcpus=vcpus, mem_mib=mem_mib, guest_mac=guest_mac,
            data_drive=data_drive,
        ),
        indent=2,
    ))

    info(f"booting microVM {name} on {tap} (guest {guest_ip})")
    log_fh = console_log.open("wb")
    process = subprocess.Popen(
        ["firecracker", "--no-api", "--config-file", str(config_path)],
        stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=detached,
    )
    return VmHandle(process=process, guest_ip=guest_ip, console_log=console_log)


def wait_for_ssh(
    vm: VmHandle, private_key: Path, *, timeout: float = 30.0,
) -> None:
    """Poll SSH until the guest accepts a command or the deadline
    passes. Dies (with the console tail) if the VMM exits early or the
    guest never comes up — a boot failure must be loud, not a hang."""
    deadline = time.monotonic() + timeout
    probe = util.ssh_base_argv(private_key, vm.guest_ip) + ["true"]
    while time.monotonic() < deadline:
        if not vm.is_alive():
            die(f"microVM exited during boot (rc={vm.process.returncode}).\n"
                f"{_console_tail(vm.console_log)}")
        result = subprocess.run(
            probe, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(0.5)
    die(f"microVM {vm.guest_ip} did not accept SSH within {timeout:.0f}s.\n"
        f"{_console_tail(vm.console_log)}")


def _console_tail(console_log: Path, lines: int = 25) -> str:
    try:
        text = console_log.read_text(errors="replace").splitlines()
    except OSError:
        return "(no console log)"
    tail = "\n".join(text[-lines:])
    return f"--- guest console (last {lines} lines) ---\n{tail}"
