"""Unit: Firecracker backend helpers.

Covers the pieces that run without KVM/root: pool IP math + config
renderers + allocation, the SSH-backed bottle's argv construction, and
the VM boot-arg assembly.
"""

from __future__ import annotations

import base64
import os
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from bot_bottle.backend.firecracker import firecracker_vm, netpool
from bot_bottle.backend.firecracker.bottle import FirecrackerBottle


def _bottle(**kw: Any) -> FirecrackerBottle:
    defaults = dict(
        name="bot-bottle-dev-abc",
        private_key=Path("/tmp/key"),
        guest_ip="100.64.0.1",
    )
    defaults.update(kw)
    return FirecrackerBottle(**defaults)  # type: ignore[arg-type]


class TestNetpoolSlots(unittest.TestCase):
    def test_slot_ip_math_31_pairs(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_FC_IP_BASE": "100.64.0.0"}):
            s0, s1 = netpool.slot(0), netpool.slot(1)
        self.assertEqual(("bbfc0", "100.64.0.0", "100.64.0.1"),
                         (s0.iface, s0.host_ip, s0.guest_ip))
        self.assertEqual(("bbfc1", "100.64.0.2", "100.64.0.3"),
                         (s1.iface, s1.host_ip, s1.guest_ip))

    def test_guest_cidr_is_31(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_FC_IP_BASE": "100.64.0.0"}):
            self.assertEqual("100.64.0.1/31", netpool.slot(0).guest_cidr)

    def test_default_base_avoids_cgnat_and_common_private_ranges(self):
        # The default base must NOT sit in 100.64.0.0/10 (RFC-6598 CGNAT,
        # which Tailscale hands node addresses from); it's an obscure
        # RFC-1918 block instead.
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual("10.243.0.0", netpool.ip_base())

    def test_pool_size_env_override(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_FC_POOL_SIZE": "4"}):
            self.assertEqual(4, netpool.pool_size())
            self.assertEqual(4, len(netpool.all_slots()))


class TestNetpoolRenderers(unittest.TestCase):
    def test_nixos_module_is_non_invasive(self):
        # The NixOS module must NOT flip the host firewall backend or
        # hand interfaces to systemd-networkd; it brings the pool up via
        # a systemd oneshot that coexists with an iptables firewall.
        root = Path(__file__).resolve().parents[2]
        mod = (root / "nix" / "firecracker-netpool.nix").read_text()
        self.assertNotIn("networking.nftables.enable = true", mod)
        self.assertNotIn("systemd.network.enable = true", mod)
        self.assertIn("bot-bottle-firecracker-netpool", mod)  # the oneshot
        self.assertIn(netpool.NFT_TABLE, mod)
        self.assertIn('iifname != "${cfg.ifacePrefix}*" return', mod)

    def test_shell_setup_reflects_overrides(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_FC_POOL_SIZE": "3"}):
            out = netpool.render_shell_setup()
        self.assertIn("BOT_BOTTLE_FC_POOL_SIZE=3", out)
        self.assertIn("firecracker-netpool.sh up", out)

    def test_systemd_unit_pins_params_and_calls_script(self):
        # The portable install: one oneshot unit, params pinned via
        # Environment= (must not depend on $SUDO_USER at boot), delegating
        # bring-up/teardown to the bundled script.
        script = "/opt/bb/scripts/firecracker-netpool.sh"
        with patch.dict(os.environ, {"BOT_BOTTLE_FC_POOL_SIZE": "4"}):
            unit = netpool.render_systemd_unit("alice", script)
        self.assertIn("Type=oneshot", unit)
        self.assertIn("BOT_BOTTLE_FC_POOL_SIZE=4", unit)
        self.assertIn("BOT_BOTTLE_FC_OWNER=alice", unit)
        self.assertIn(f"BOT_BOTTLE_FC_IP_BASE={netpool.ip_base()}", unit)
        self.assertIn(f"ExecStart={script} up", unit)
        self.assertIn(f"ExecStop={script} down", unit)
        self.assertIn("WantedBy=multi-user.target", unit)


class TestFirecrackerStatus(unittest.TestCase):
    """`status()` gates launch: ready == TAP pool present + no overlap.
    An nft table that can't be confirmed unprivileged is reported, not
    treated as not-ready (the post-boot probe is authoritative)."""

    def _run(self):
        import contextlib
        import io
        from bot_bottle.backend.firecracker import setup as fc_setup
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = fc_setup.status()
        return rc, buf.getvalue()

    def test_ready_when_taps_present_even_if_nft_unverifiable(self):
        from bot_bottle.backend.firecracker import setup as fc_setup
        with patch.object(fc_setup.netpool, "missing_taps", return_value=[]), \
             patch.object(fc_setup.netpool, "overlapping_routes", return_value=[]), \
             patch.object(fc_setup.shutil, "which", return_value=None):
            rc, out = self._run()
        self.assertEqual(0, rc)
        self.assertIn("unverified", out)

    def test_not_ready_when_taps_missing(self):
        from bot_bottle.backend.firecracker import setup as fc_setup
        with patch.object(fc_setup.netpool, "missing_taps", return_value=["bbfc0"]), \
             patch.object(fc_setup.netpool, "overlapping_routes", return_value=[]), \
             patch.object(fc_setup.shutil, "which", return_value=None):
            rc, _ = self._run()
        self.assertEqual(1, rc)

    def test_not_ready_on_range_overlap(self):
        from bot_bottle.backend.firecracker import netpool
        from bot_bottle.backend.firecracker import setup as fc_setup
        conflict = netpool.RouteConflict(dst="10.243.0.0/24", dev="eth0")
        with patch.object(fc_setup.netpool, "missing_taps", return_value=[]), \
             patch.object(fc_setup.netpool, "overlapping_routes", return_value=[conflict]), \
             patch.object(fc_setup.shutil, "which", return_value=None):
            rc, out = self._run()
        self.assertEqual(1, rc)
        self.assertIn("CLASHES", out)


class TestNetpoolOverlap(unittest.TestCase):
    """`overlapping_routes()` flags a pool base that collides with an
    existing host route (Tailscale CGNAT peer, docker/libvirt bridge,
    LAN) and ignores our own bbfc TAPs + the default route."""

    def _routes(self, entries: list[dict]) -> object:
        import json
        import subprocess

        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(entries), stderr="",
            )
        return patch.object(netpool.subprocess, "run", side_effect=fake_run)

    def test_flags_route_overlapping_pool(self):
        # base 100.64.0.0 + a Tailscale peer /32 inside the pool window.
        with patch.dict(os.environ, {"BOT_BOTTLE_FC_IP_BASE": "100.64.0.0",
                                     "BOT_BOTTLE_FC_POOL_SIZE": "8"}), \
             self._routes([
                 {"dst": "default", "dev": "enp4s0"},
                 {"dst": "100.64.0.5", "dev": "tailscale0"},
             ]):
            conflicts = netpool.overlapping_routes()
        self.assertEqual(1, len(conflicts))
        self.assertEqual("tailscale0", conflicts[0].dev)

    def test_ignores_own_taps_and_default(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_FC_IP_BASE": "10.243.0.0",
                                     "BOT_BOTTLE_FC_POOL_SIZE": "8"}), \
             self._routes([
                 {"dst": "default", "dev": "enp4s0"},
                 {"dst": "10.243.0.0/31", "dev": "bbfc0"},
                 {"dst": "192.168.1.0/24", "dev": "enp4s0"},
             ]):
            self.assertEqual([], netpool.overlapping_routes())

    def test_empty_when_ip_missing(self):
        with self._routes([]) as run:
            run.side_effect = FileNotFoundError()
            self.assertEqual([], netpool.overlapping_routes())


class TestNetpoolAllocation(unittest.TestCase):
    def test_allocate_is_mutually_exclusive(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_FC_POOL_SIZE": "1"}):
            # First allocation grabs the only slot; the lock is held
            # until the handle is closed, so a second call must fail
            # over (and here, exhaust the pool).
            slot, lock = netpool.allocate("first")
            self.addCleanup(lock.close)
            self.assertEqual("bbfc0", slot.iface)
            with patch.object(netpool, "die",
                              side_effect=SystemExit("exhausted")):
                with self.assertRaises(SystemExit):
                    netpool.allocate("second")

    def test_allocate_releases_on_close(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_FC_POOL_SIZE": "1"}):
            slot, lock = netpool.allocate("first")
            lock.close()
            # Slot is free again after close.
            slot2, lock2 = netpool.allocate("second")
            self.addCleanup(lock2.close)
            self.assertEqual(slot.iface, slot2.iface)


class TestBottleAgentArgv(unittest.TestCase):
    def test_interactive_uses_ssh_tty_and_runuser(self):
        argv = _bottle().agent_argv([], tty=True)
        self.assertEqual("ssh", argv[0])
        self.assertIn("-t", argv)
        self.assertIn("100.64.0.1", " ".join(argv))
        idx = argv.index("--")
        self.assertEqual(["runuser", "-u", "node", "--"], argv[idx + 1:idx + 5])
        self.assertIn("claude", argv)

    def test_non_interactive_has_no_tty(self):
        argv = _bottle().agent_argv([], tty=False)
        self.assertEqual("ssh", argv[0])
        self.assertNotIn("-t", argv)

    def test_appends_extra_args_after_command(self):
        argv = _bottle().agent_argv(
            ["--dangerously-skip-permissions", "--continue"], tty=False,
        )
        idx = argv.index("claude")
        self.assertEqual(
            ["claude", "--dangerously-skip-permissions", "--continue"],
            argv[idx:],
        )

    def test_prompt_file_flag_injected(self):
        argv = _bottle(
            prompt_path_in_guest="/home/node/.bot-bottle-prompt.txt",
        ).agent_argv(["--continue"], tty=False)
        idx = argv.index("claude")
        self.assertEqual(
            ["claude", "--continue",
             "--append-system-prompt-file", "/home/node/.bot-bottle-prompt.txt"],
            argv[idx:],
        )

    def test_workdir_sets_chdir(self):
        # The agent runs from its workdir via `env --chdir` (ssh-safe;
        # not a `sh -c 'cd …'` wrapper, which the ssh arg-join mangles).
        argv = _bottle(agent_workdir="/home/node/workspace").agent_argv([], tty=False)
        self.assertIn("--chdir=/home/node/workspace", argv)

    def test_default_workdir_still_chdirs_off_root(self):
        # Even with the default workdir the agent must leave /root (the
        # root-SSH cwd it can't read); it cd's to /home/node.
        argv = _bottle().agent_argv([], tty=False)
        self.assertIn("--chdir=/home/node", argv)

    def test_guest_env_injected(self):
        argv = _bottle(guest_env={"HTTPS_PROXY": "http://100.64.0.0:9099"}).agent_argv(
            [], tty=False,
        )
        self.assertIn("HTTPS_PROXY=http://100.64.0.0:9099", argv)
        self.assertIn("HOME=/home/node", argv)
        self.assertIn("USER=node", argv)


class TestSetupScriptConsistency(unittest.TestCase):
    """The shell setup script duplicates the pool defaults; keep them in
    lockstep with the Python constants so the two setup paths agree."""

    def _script(self) -> str:
        root = Path(__file__).resolve().parent.parent.parent
        return (root / "scripts" / "firecracker-netpool.sh").read_text()

    def test_defaults_match_python(self):
        script = self._script()
        with patch.dict(os.environ, {}, clear=True):
            self.assertIn(f'POOL_SIZE:-{netpool.pool_size()}', script)
            self.assertIn(f'BOT_BOTTLE_FC_IP_BASE:-{netpool.ip_base()}', script)
            self.assertIn(f'IFACE_PREFIX:-{netpool.IFACE_PREFIX}', script)

    def test_table_name_and_ports_match(self):
        script = self._script()
        self.assertIn(f'TABLE="{netpool.NFT_TABLE}"', script)
        for port in netpool.SIDECAR_PORTS:
            self.assertIn(str(port), script)


class TestBootArgs(unittest.TestCase):
    def test_boot_args_carry_ip_and_pubkey(self):
        args = firecracker_vm._boot_args(
            guest_ip="100.64.0.1", host_ip="100.64.0.0", pubkey="ssh-ed25519 AAAA x",
        )
        self.assertIn("ip=100.64.0.1::100.64.0.0:255.255.255.254::eth0:off", args)
        self.assertIn("init=/bb-init", args)
        b64 = base64.b64encode(b"ssh-ed25519 AAAA x").decode()
        self.assertIn(f"bb_pubkey={b64}", args)

    def test_config_has_rootfs_and_tap(self):
        cfg = cast(Any, firecracker_vm._config(
            rootfs=Path("/run/rootfs.ext4"), tap="bbfc0",
            guest_ip="100.64.0.1", host_ip="100.64.0.0", pubkey="k",
            vcpus=2, mem_mib=2048, guest_mac="06:00:AC:10:00:02",
        ))
        self.assertEqual("/run/rootfs.ext4", cfg["drives"][0]["path_on_host"])
        self.assertFalse(cfg["drives"][0]["is_read_only"])
        self.assertEqual("bbfc0", cfg["network-interfaces"][0]["host_dev_name"])


if __name__ == "__main__":
    unittest.main()
