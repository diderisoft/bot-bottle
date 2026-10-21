"""Unit: Firecracker backend helpers.

Covers the pieces that run without KVM/root: pool IP math + config
renderers + allocation, the SSH-backed bottle's argv construction, and
the VM boot-arg assembly.
"""

from __future__ import annotations

import base64
import json
import subprocess
import unittest
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

from bot_bottle.contract.hostctl import Settings
from bot_bottle.secret_source import MappingSecrets
from bot_bottle.backend.firecracker import firecracker_vm, netpool
from bot_bottle.contract.hostctl import InfraSettings, Settings
from bot_bottle.backend.firecracker.bottle import FirecrackerBottle


def _bottle(**kw: Any) -> FirecrackerBottle:
    defaults = dict(
        name="bot-bottle-dev-abc",
        private_key=Path("/tmp/key"),
        guest_ip="100.64.0.1",
    )
    defaults.update(kw)
    return FirecrackerBottle(**defaults)  # type: ignore[arg-type]


def _pool(**fields: Any) -> netpool.NetPool:
    return netpool.from_settings(InfraSettings(**fields))


class TestNetpoolSlots(unittest.TestCase):
    def test_slot_ip_math_31_pairs(self):
        pool = _pool(fc_ip_base="100.64.0.0")
        s0, s1 = pool.slot(0), pool.slot(1)
        pfx = pool.iface_prefix
        self.assertEqual((f"{pfx}0", "100.64.0.0", "100.64.0.1"),
                         (s0.iface, s0.host_ip, s0.guest_ip))
        self.assertEqual((f"{pfx}1", "100.64.0.2", "100.64.0.3"),
                         (s1.iface, s1.host_ip, s1.guest_ip))

    def test_guest_cidr_is_31(self):
        self.assertEqual("100.64.0.1/31", _pool(fc_ip_base="100.64.0.0").slot(0).guest_cidr)

    def test_default_base_avoids_cgnat_and_common_private_ranges(self):
        # The default base must NOT sit in 100.64.0.0/10 (RFC-6598 CGNAT,
        # which Tailscale hands node addresses from); it's an obscure
        # RFC-1918 block instead.
        self.assertEqual("10.243.0.0", _pool().ip_base)

    def test_pool_size_override(self):
        pool = _pool(fc_pool_size=4)
        self.assertEqual(4, pool.pool_size)
        self.assertEqual(4, len(pool.all_slots()))

    def test_iface_overrides(self):
        pool = _pool(fc_iface_prefix="bbci", fc_orch_iface="bbciorch0",
                     fc_gw_iface="bbcigw0", fc_nft_table="bb_ci")
        self.assertEqual("bbci3", pool.slot(3).iface)
        self.assertEqual("bbciorch0", pool.orch_slot().iface)
        self.assertEqual("bbcigw0", pool.gw_slot().iface)
        self.assertEqual("bb_ci", pool.nft_table)


class TestNetpoolRenderers(unittest.TestCase):
    def test_guest_init_restores_node_home_boundary(self):
        from bot_bottle.backend.firecracker import util

        self.assertIn("chown node:node /home/node", util._GUEST_INIT)
        self.assertIn("chmod 755 /home/node", util._GUEST_INIT)
        self.assertIn("mount -t tmpfs -o mode=0755 tmpfs /run", util._GUEST_INIT)

    def test_nixos_module_is_non_invasive(self):
        # The NixOS module must NOT flip the host firewall backend or
        # hand interfaces to systemd-networkd; it brings the pool up via
        # a systemd oneshot that coexists with an iptables firewall.
        root = Path(__file__).resolve().parents[2]
        mod = (root / "nix" / "firecracker-netpool.nix").read_text()
        self.assertNotIn("networking.nftables.enable = true", mod)
        self.assertNotIn("systemd.network.enable = true", mod)
        self.assertIn("bot-bottle-firecracker-netpool", mod)  # the oneshot
        # Supports group-owned TAPs (shared pool for a multi-user host).
        self.assertIn("BOT_BOTTLE_FC_GROUP", mod)

    def test_nixos_module_delegates_and_holds_no_literals(self):
        # Single-source discipline: the module must NOT re-implement the
        # bring-up (it delegates to the shared script) and must NOT hard-
        # code the pool defaults (it readFile-parses the shared .env), so
        # nothing can drift from netpool.py / the shell script.
        root = Path(__file__).resolve().parents[2]
        mod = (root / "nix" / "firecracker-netpool.nix").read_text()
        # Delegation to the one bring-up implementation + shared defaults.
        self.assertIn("scripts/firecracker-netpool.sh", mod)
        self.assertIn("netpool.defaults.env", mod)
        self.assertIn("BOT_BOTTLE_FC_NFT_TABLE", mod)
        # No duplicated literals or nft ruleset.
        self.assertNotIn(_pool().ip_base, mod)        # 10.243.0.0
        self.assertNotIn(_pool().nft_table, mod)      # bot_bottle_fc
        self.assertNotIn("ct status dnat", mod)       # the nft ruleset

    def test_shell_setup_reflects_overrides(self):
        out = netpool.render_shell_setup(_pool(fc_pool_size=3))
        self.assertIn("BOT_BOTTLE_FC_POOL_SIZE=3", out)
        self.assertIn("firecracker-netpool.sh up", out)

    def test_systemd_unit_pins_params_and_calls_script(self):
        # The portable install: one oneshot unit, params pinned via
        # Environment= (must not depend on $SUDO_USER at boot), delegating
        # bring-up/teardown to the bundled script.
        script = "/opt/bb/scripts/firecracker-netpool.sh"
        pool = _pool(fc_pool_size=4)
        unit = netpool.render_systemd_unit(pool, "alice", script)
        self.assertIn("Type=oneshot", unit)
        self.assertIn("BOT_BOTTLE_FC_POOL_SIZE=4", unit)
        self.assertIn("BOT_BOTTLE_FC_OWNER=alice", unit)
        self.assertIn(f"BOT_BOTTLE_FC_IP_BASE={pool.ip_base}", unit)
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
            rc = fc_setup.status(Settings())
        return rc, buf.getvalue()

    def _stub_artifacts(self, fc_setup: object) -> tuple[MagicMock, MagicMock]:
        k: MagicMock = MagicMock()
        k.is_file.return_value = True
        d: MagicMock = MagicMock()
        d.is_file.return_value = True
        return k, d

    def test_ready_when_taps_present_even_if_nft_unverifiable(self):
        from bot_bottle.backend.firecracker import setup as fc_setup

        def _which(cmd: str) -> str | None:
            return None if cmd == "nft" else f"/usr/bin/{cmd}"

        k, d = self._stub_artifacts(fc_setup)
        with patch.object(netpool.NetPool, "missing_taps", return_value=[]), \
             patch.object(netpool.NetPool, "overlapping_routes", return_value=[]), \
             patch.object(fc_setup.shutil, "which", side_effect=_which), \
             patch.object(fc_setup, "_firecracker_binary_ok", return_value=True), \
             patch.object(fc_setup, "_kvm_accessible", return_value=True), \
             patch.object(fc_setup.util, "kernel_path", return_value=k), \
             patch.object(fc_setup.util, "dropbear_path", return_value=d):
            rc, out = self._run()
        self.assertEqual(0, rc)
        self.assertIn("unverified", out)

    def test_not_ready_when_taps_missing(self):
        from bot_bottle.backend.firecracker import setup as fc_setup
        k, d = self._stub_artifacts(fc_setup)
        with patch.object(netpool.NetPool, "missing_taps", return_value=["bbfc0"]), \
             patch.object(netpool.NetPool, "overlapping_routes", return_value=[]), \
             patch.object(fc_setup.shutil, "which", return_value=None), \
             patch.object(fc_setup, "_firecracker_binary_ok", return_value=True), \
             patch.object(fc_setup, "_kvm_accessible", return_value=True), \
             patch.object(fc_setup.util, "kernel_path", return_value=k), \
             patch.object(fc_setup.util, "dropbear_path", return_value=d):
            rc, _ = self._run()
        self.assertEqual(1, rc)

    def test_not_ready_on_range_overlap(self):
        from bot_bottle.backend.firecracker import netpool
        from bot_bottle.backend.firecracker import setup as fc_setup
        k, d = self._stub_artifacts(fc_setup)
        conflict = netpool.RouteConflict(dst="10.243.0.0/24", dev="eth0")
        with patch.object(netpool.NetPool, "missing_taps", return_value=[]), \
             patch.object(netpool.NetPool, "overlapping_routes", return_value=[conflict]), \
             patch.object(fc_setup.shutil, "which", return_value=None), \
             patch.object(fc_setup, "_firecracker_binary_ok", return_value=True), \
             patch.object(fc_setup, "_kvm_accessible", return_value=True), \
             patch.object(fc_setup.util, "kernel_path", return_value=k), \
             patch.object(fc_setup.util, "dropbear_path", return_value=d):
            rc, out = self._run()
        self.assertEqual(1, rc)
        self.assertIn("CLASHES", out)


class TestNetpoolOverlap(unittest.TestCase):
    """`overlapping_routes()` flags a pool base that collides with an
    existing host route (Tailscale CGNAT peer, docker/libvirt bridge,
    LAN) and ignores our own bbfc TAPs + the default route."""

    def _routes(
        self, entries: list[dict[str, str]]
    ) -> AbstractContextManager[MagicMock]:
        def fake_run(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(entries), stderr="",
            )
        return patch.object(netpool.subprocess, "run", side_effect=fake_run)

    def test_flags_route_overlapping_pool(self):
        # base 100.64.0.0 + a Tailscale peer /32 inside the pool window.
        with self._routes([
                 {"dst": "default", "dev": "enp4s0"},
                 {"dst": "100.64.0.5", "dev": "tailscale0"},
             ]):
            conflicts = _pool(fc_ip_base="100.64.0.0", fc_pool_size=8).overlapping_routes()
        self.assertEqual(1, len(conflicts))
        self.assertEqual("tailscale0", conflicts[0].dev)

    def test_ignores_own_taps_and_default(self):
        pool = _pool(fc_ip_base="10.243.0.0", fc_pool_size=8)
        with self._routes([
                 {"dst": "default", "dev": "enp4s0"},
                 {"dst": "10.243.0.0/31", "dev": pool.slot(0).iface},
                 {"dst": "192.168.1.0/24", "dev": "enp4s0"},
             ]):
            self.assertEqual([], pool.overlapping_routes())

    def test_empty_when_ip_missing(self):
        with self._routes([]) as run:
            run.side_effect = FileNotFoundError()
            self.assertEqual([], _pool().overlapping_routes())


class TestNetpoolAllocation(unittest.TestCase):
    # A prefix of its own, so the test's lock files can't collide with a real pool.
    _POOL = _pool(fc_pool_size=1, fc_iface_prefix="bbunit")

    def test_allocate_is_mutually_exclusive(self):
        # First allocation grabs the only slot; the lock is held
        # until the handle is closed, so a second call must fail
        # over (and here, exhaust the pool).
        slot, lock = self._POOL.allocate("first")
        self.addCleanup(lock.close)
        self.assertEqual(self._POOL.slot(0).iface, slot.iface)
        with patch.object(netpool, "die",
                          side_effect=SystemExit("exhausted")):
            with self.assertRaises(SystemExit):
                self._POOL.allocate("second")

    def test_allocate_releases_on_close(self):
        slot, lock = self._POOL.allocate("first")
        lock.close()
        # Slot is free again after close.
        slot2, lock2 = self._POOL.allocate("second")
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

    def test_codex_multiword_prompt_survives_ssh_reparse(self):
        # codex's read_prompt_file mode passes a single positional with
        # spaces ("Read and follow the instructions in <path>."). ssh
        # space-joins the remote argv and the guest shell re-splits it, so
        # the token MUST be quoted or codex sees "and" as a subcommand
        # (regression). Each remote token is shlex.quote'd; round-tripping
        # the joined remote command back through shlex.split must recover
        # the prompt as ONE argument.
        import shlex

        argv = _bottle(
            agent_command="codex",
            agent_prompt_mode="read_prompt_file",
            agent_provider_template="codex",
            prompt_path_in_guest="/home/node/.bot-bottle-prompt.txt",
        ).agent_argv([], tty=False)
        idx = argv.index("--")
        remote_line = " ".join(argv[idx + 1:])  # what ssh sends to the guest
        reparsed = shlex.split(remote_line)  # what the guest shell sees
        prompt = "Read and follow the instructions in /home/node/.bot-bottle-prompt.txt."
        self.assertIn(prompt, reparsed)
        # codex is the last simple token before the (single) prompt arg.
        self.assertEqual([*reparsed[reparsed.index("codex"):]], ["codex", prompt])

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


class TestNetpoolDefaultsSingleSource(unittest.TestCase):
    """The pool defaults live in one shared file (netpool.defaults.env);
    Python parses it and the shell script + Nix module read the same file,
    so the three setup paths can't drift."""

    def _root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    def _shared_defaults(self) -> dict[str, str]:
        text = netpool.DEFAULTS_FILE.read_text()
        out: dict[str, str] = {}
        for raw in text.splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
        return out

    def test_python_reads_the_shared_file(self):
        d = self._shared_defaults()
        pool = _pool()
        self.assertEqual(int(d["BOT_BOTTLE_FC_POOL_SIZE"]), pool.pool_size)
        self.assertEqual(d["BOT_BOTTLE_FC_IP_BASE"], pool.ip_base)
        self.assertEqual(d["BOT_BOTTLE_FC_IFACE_PREFIX"], pool.iface_prefix)
        self.assertEqual(d["BOT_BOTTLE_FC_NFT_TABLE"], pool.nft_table)
        self.assertEqual(d["BOT_BOTTLE_FC_ORCH_IFACE"], pool.orch_iface)
        self.assertEqual(d["BOT_BOTTLE_FC_GW_IFACE"], pool.gw_iface)

    def test_settings_override_the_shared_default(self):
        self.assertEqual("10.99.0.0", _pool(fc_ip_base="10.99.0.0").ip_base)

    def test_script_defers_to_shared_file_without_literals(self):
        script = (self._root() / "scripts" / "firecracker-netpool.sh").read_text()
        # Delegates its defaults to the shared file, not hardcoded values.
        self.assertIn("netpool.defaults.env", script)
        self.assertIn("BOT_BOTTLE_FC_POOL_SIZE:-$(_default BOT_BOTTLE_FC_POOL_SIZE)", script)
        self.assertIn('TABLE="${BOT_BOTTLE_FC_NFT_TABLE:-$(_default BOT_BOTTLE_FC_NFT_TABLE)}"', script)


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
            kernel=Path("/k/vmlinux"), rootfs=Path("/run/rootfs.ext4"), tap="bbfc0",
            guest_ip="100.64.0.1", host_ip="100.64.0.0", pubkey="k",
            vcpus=2, mem_mib=2048, guest_mac="06:00:AC:10:00:02",
        ))
        self.assertEqual("/run/rootfs.ext4", cfg["drives"][0]["path_on_host"])
        self.assertFalse(cfg["drives"][0]["is_read_only"])
        self.assertEqual("bbfc0", cfg["network-interfaces"][0]["host_dev_name"])
        self.assertEqual("/k/vmlinux", cfg["boot-source"]["kernel_image_path"])
        self.assertEqual(1, len(cfg["drives"]))  # no data drive by default

    def test_config_adds_data_drives_in_order(self):
        cfg = cast(Any, firecracker_vm._config(
            kernel=Path("/k/vmlinux"), rootfs=Path("/run/rootfs.ext4"), tap="bbfc0",
            guest_ip="100.64.0.1", host_ip="100.64.0.0", pubkey="k",
            vcpus=2, mem_mib=2048, guest_mac="06:00:AC:10:00:02",
            data_drives=(Path("/run/registry.ext4"), Path("/run/build-store.ext4")),
        ))
        self.assertEqual(3, len(cfg["drives"]))
        # Guest device order follows the list: /dev/vdb, then /dev/vdc.
        self.assertEqual(["/run/registry.ext4", "/run/build-store.ext4"],
                         [d["path_on_host"] for d in cfg["drives"][1:]])
        self.assertFalse(any(d["is_root_device"] for d in cfg["drives"][1:]))
        self.assertEqual(2, len({d["drive_id"] for d in cfg["drives"][1:]}))


class TestBottleExecClose(unittest.TestCase):
    def test_exec_runs_as_node_and_returns_result(self):
        from bot_bottle.backend.firecracker import bottle as bmod
        with patch.object(bmod.subprocess, "run",
                          return_value=bmod.subprocess.CompletedProcess(
                              [], 0, stdout="hi\n", stderr="")) as run:
            r = _bottle().exec("echo hi")
        self.assertEqual(0, r.returncode)
        self.assertEqual("hi\n", r.stdout)
        # runs `runuser -u node` over ssh, script piped on stdin.
        self.assertIn("runuser", run.call_args.args[0])
        self.assertEqual("echo hi", run.call_args.kwargs["input"])

    def test_exec_respects_root_user(self):
        from bot_bottle.backend.firecracker import bottle as bmod
        with patch.object(bmod.subprocess, "run",
                          return_value=bmod.subprocess.CompletedProcess([], 0,
                                                                        stdout="", stderr="")) as run:
            _bottle().exec("id", user="root")
        joined = " ".join(run.call_args.args[0])
        self.assertIn("runuser -u root", joined)

    def test_close_is_idempotent_noop(self):
        b = _bottle()
        b.close()
        b.close()  # must not raise


class TestBottlePlanProperties(unittest.TestCase):
    def _plan(self, **overrides: Any):
        from bot_bottle.backend.firecracker.bottle_plan import FirecrackerBottlePlan
        ap = cast(Any, MagicMock())
        ap.instance_name = "bot-bottle-demo-x"
        ap.image = "bot-bottle-claude:latest"
        ap.dockerfile = "Dockerfile.claude"
        ap.prompt_file = Path("/stage/prompt.txt")
        ap.command = "claude"
        ap.prompt_mode = "append_file"
        ap.template = "claude"
        ap.guest_home = "/home/node"
        ap.guest_env = {}
        egress_plan = cast(Any, MagicMock())
        egress_plan.canary = ""
        egress_plan.canary_env = ""
        fields = dict(
            spec=cast(Any, MagicMock()), manifest=cast(Any, MagicMock()),
            stage_dir=Path("/stage"), git_gate_plan=cast(Any, MagicMock()),
            egress_plan=egress_plan, supervise_plan=None,
            agent_provision=ap, slug="demo-x", forwarded_env={},
            settings=Settings(),
            secrets=MappingSecrets({}),
        )
        fields.update(overrides)
        return FirecrackerBottlePlan(**fields)  # type: ignore[arg-type]

    def test_provision_backed_properties(self):
        p = self._plan()
        self.assertEqual("bot-bottle-demo-x", p.container_name)
        self.assertEqual("bot-bottle-claude:latest", p.image)
        self.assertEqual("Dockerfile.claude", p.dockerfile_path)
        self.assertEqual(Path("/stage/prompt.txt"), p.prompt_file)
        self.assertEqual("claude", p.agent_command)
        self.assertEqual("append_file", p.agent_prompt_mode)
        self.assertEqual("claude", p.agent_provider_template)

    def test_git_gate_insteadof_defaults(self):
        p = self._plan()
        self.assertEqual("git-gate", p.git_gate_insteadof_host)
        self.assertEqual("git", p.git_gate_insteadof_scheme)

    def test_git_gate_insteadof_http_override(self):
        p = self._plan(agent_git_gate_url="http://10.243.0.0:9420/")
        self.assertEqual("10.243.0.0:9420", p.git_gate_insteadof_host)
        self.assertEqual("http", p.git_gate_insteadof_scheme)

    def test_guest_env_pins_global_git_config(self):
        from bot_bottle.backend.firecracker.launch import _agent_guest_env

        env = _agent_guest_env(self._plan(), "10.243.0.0")
        self.assertEqual("/home/node/.gitconfig", env["GIT_CONFIG_GLOBAL"])


if __name__ == "__main__":
    unittest.main()
