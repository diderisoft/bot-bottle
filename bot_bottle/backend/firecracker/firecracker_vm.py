"""Firecracker microVM process lifecycle.

A VM is one `firecracker --no-api` process booted from a JSON config (kernel,
writable rootfs, one TAP). Teardown is a signal; the guest dies with its VMM.
The guest takes its IP from the kernel `ip=` arg and its SSH pubkey from
`bb_pubkey=`.
"""

from __future__ import annotations

import base64
import json
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ...lib.log import die, info
from . import util


# Console captured to a log; standard Firecracker args; `i8042.*` skip the
# absent PS/2 probe.
_BASE_BOOT_ARGS = (
    "console=ttyS0 reboot=k panic=1 pci=off "
    "i8042.noaux i8042.nomux i8042.nopnp i8042.dumbkbd "
    "root=/dev/vda rw init=/bb-init"
)


@dataclass
class VmHandle:
    """A running microVM: VMM process, guest IP, console log."""

    process: subprocess.Popen[bytes]
    guest_ip: str
    console_log: Path

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def terminate(self) -> None:
        """Stop the VMM: SIGTERM, then SIGKILL."""
        if self.process.poll() is not None:
            return
        self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def _boot_args(
    guest_ip: str, host_ip: str, pubkey: str, extra: str = "",
) -> str:
    # /31 point-to-point: netmask 255.255.255.254, gateway is the host TAP IP.
    ip_arg = f"ip={guest_ip}::{host_ip}:255.255.255.254::eth0:off"
    pub_b64 = base64.b64encode(pubkey.encode()).decode()
    args = f"{_BASE_BOOT_ARGS} {ip_arg} bb_pubkey={pub_b64}"
    # Extra cmdline params the guest init reads (e.g. the gateway VM's
    # `bb_orch=`).
    return f"{args} {extra}".rstrip() if extra else args


def _config(
    *,
    kernel: Path,
    rootfs: Path,
    tap: str,
    guest_ip: str,
    host_ip: str,
    pubkey: str,
    vcpus: int,
    mem_mib: int,
    guest_mac: str,
    data_drives: Sequence[Path] = (),
    extra_boot_args: str = "",
) -> dict[str, object]:
    drives: list[dict[str, object]] = [
        {
            "drive_id": "rootfs",
            "path_on_host": str(rootfs),
            "is_root_device": True,
            "is_read_only": False,
        }
    ]
    # Extra virtio-block devices (/dev/vdb, ... in order): an infra VM's
    # persistent volumes.
    for i, drive in enumerate(data_drives):
        drives.append({
            "drive_id": f"data{i}",
            "path_on_host": str(drive),
            "is_root_device": False,
            "is_read_only": False,
        })
    return {
        "boot-source": {
            "kernel_image_path": str(kernel),
            "boot_args": _boot_args(guest_ip, host_ip, pubkey, extra_boot_args),
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
    kernel: Path,
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
    data_drives: Sequence[Path] = (),
    extra_boot_args: str = "",
) -> VmHandle:
    """Write the config and spawn the VMM; callers wait for SSH. `detached`
    puts it in its own session so it outlives the launcher (the infra VMs)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    config_path = run_dir / "config.json"
    console_log = run_dir / "console.log"
    config_path.write_text(json.dumps(
        _config(
            kernel=kernel, rootfs=rootfs, tap=tap, guest_ip=guest_ip, host_ip=host_ip,
            pubkey=pubkey, vcpus=vcpus, mem_mib=mem_mib, guest_mac=guest_mac,
            data_drives=data_drives, extra_boot_args=extra_boot_args,
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


def tap_of(run_dir: Path) -> str:
    """The TAP device the VM in `run_dir` was booted on, or "" if its config
    can't be read."""
    try:
        config = json.loads((run_dir / "config.json").read_text())
        return str(config["network-interfaces"][0]["host_dev_name"])
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return ""


def wait_for_ssh(
    vm: VmHandle, private_key: Path, *, timeout: float = 30.0,
) -> None:
    """Poll SSH until the guest answers. Dies with the console tail if the VMM
    exits or the guest never comes up."""
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
