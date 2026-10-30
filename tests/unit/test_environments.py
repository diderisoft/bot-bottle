"""Unit: named environments (`BOT_BOTTLE_ENV`): validation, what each
environment derives, live's values unchanged, the slot registry, the
firecracker slice math (Python and the setup script agree), and `envs`
listing and removal planning with the host mocked out."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle import environment as env
from bot_bottle.backend.firecracker import netpool
from bot_bottle.contract.hostctl import InfraSettings
from bot_bottle.lib.log import Die

REPO = Path(__file__).resolve().parents[2]


class _TmpHome(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


class TestValidation(unittest.TestCase):
    def test_good_names(self) -> None:
        for name in ("live", "dev", "ci-6839", "ci-12345678901", "a", "x1-y2", "a" * 24):
            with self.subTest(name=name):
                self.assertEqual(name, env.validate(name))

    def test_bad_names(self) -> None:
        for name in ("", "Dev", "dev_2", "-dev", "dev-", "dev.2", "dev 2", "a" * 25, "ü"):
            with self.subTest(name=name):
                with self.assertRaises(env.InvalidEnvironmentError):
                    env.validate(name)

    def test_active_name_defaults_to_live(self) -> None:
        self.assertEqual("live", env.active_name({}))
        self.assertEqual("live", env.active_name({"BOT_BOTTLE_ENV": "  "}))
        self.assertEqual("dev", env.active_name({"BOT_BOTTLE_ENV": "dev"}))
        with self.assertRaises(env.InvalidEnvironmentError):
            env.active_name({"BOT_BOTTLE_ENV": "Nope"})

    def test_the_cli_stops_on_a_bad_name(self) -> None:
        from bot_bottle.cli import main  # pylint: disable=import-outside-toplevel
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"BOT_BOTTLE_ENV": "Bad_Name"}), \
                contextlib.redirect_stderr(err):
            self.assertEqual(2, main(["status"]))
        self.assertIn("BOT_BOTTLE_ENV='Bad_Name'", err.getvalue())


class TestLiveIsUnchanged(unittest.TestCase):
    """Live derives byte-for-byte what bot-bottle used before environments."""

    def test_pure_derivations(self) -> None:
        home = Path("/home/u")
        self.assertEqual(home / ".bot-bottle", env.default_root("live", home))
        self.assertEqual("bot-bottle-", env.resource_prefix("live"))
        self.assertEqual("10.242.255.0/24", env.docker_gateway_subnet("live"))
        self.assertEqual("10.242.0.0/20", env.docker_agent_subnets("live"))
        self.assertEqual("io.bot-bottle", env.reverse_dns("live"))
        self.assertEqual("fd62:6f74:6274:6c65::/64", env.macos_agent_subnet_v6("live"))

    def test_root(self) -> None:
        from bot_bottle.paths import bot_bottle_root  # pylint: disable=import-outside-toplevel
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BOT_BOTTLE_ROOT", None)
            self.assertEqual(Path.home() / ".bot-bottle", bot_bottle_root())

    def test_resource_names(self) -> None:
        # pylint: disable=import-outside-toplevel
        from bot_bottle.backend import base
        from bot_bottle.backend.docker import agent_network, compose, gateway as dgw
        from bot_bottle.backend.docker import orchestrator as dorch
        from bot_bottle.backend.firecracker import gateway as fgw, orchestrator as forch
        from bot_bottle.backend.macos_container import enumerate as menum
        from bot_bottle.backend.macos_container import gateway as mgw, host_firewall
        from bot_bottle.backend.macos_container import orchestrator as morch
        from bot_bottle.control import gateway as cgw
        from bot_bottle.control.hostctl import service_unit
        expected = {
            cgw.GATEWAY_NAME: "bot-bottle-orch-gateway",
            cgw.GATEWAY_LABEL: "bot-bottle-orch-gateway=1",
            cgw.GATEWAY_NETWORK: "bot-bottle-gateway",
            dorch.ORCHESTRATOR_NAME: "bot-bottle-orchestrator",
            dorch.ORCHESTRATOR_LABEL: "bot-bottle-orchestrator=1",
            dorch.ORCHESTRATOR_NETWORK: "bot-bottle-orchestrator",
            dorch.ORCHESTRATOR_PUBLISH_NETWORK: "bot-bottle-orchestrator-publish",
            dgw.GATEWAY_EGRESS_NETWORK: "bot-bottle-gateway-egress",
            agent_network.AGENT_NETWORK_PREFIX: "bot-bottle-agent-",
            compose.COMPOSE_PROJECT_PREFIX: "bot-bottle-",
            compose.compose_project_name("s1"): "bot-bottle-s1",
            base.home_volume_name("s1"): "bot-bottle-home-s1",
            morch.ORCHESTRATOR_NAME: "bot-bottle-mac-orchestrator",
            morch.ORCHESTRATOR_LABEL: "bot-bottle-mac-orchestrator=1",
            morch.ORCHESTRATOR_DB_VOLUME: "bot-bottle-mac-db",
            mgw.GATEWAY_NETWORK: "bot-bottle-mac-gateway",
            mgw.GATEWAY_EGRESS_NETWORK: "bot-bottle-mac-egress",
            mgw.CONTROL_NETWORK: "bot-bottle-mac-control",
            mgw.GATEWAY_NAME: "bot-bottle-mac-infra",
            mgw.GATEWAY_LABEL: "bot-bottle-mac-infra=1",
            mgw.AGENT_SUBNET_V6: "fd62:6f74:6274:6c65::/64",
            menum.CONTAINER_NAME_PREFIX: "bot-bottle-",
            forch.ORCHESTRATOR_NAME: "bot-bottle-orchestrator",
            fgw.GATEWAY_NAME: "bot-bottle-gateway",
            host_firewall.ANCHOR: "com.apple/io.bot-bottle",
            str(host_firewall.RULES_PATH): "/etc/pf.anchors/io.bot-bottle",
            host_firewall.DAEMON_LABEL: "io.bot-bottle.pf",
            service_unit.LAUNCHD_LABEL: "io.bot-bottle.hostctl",
            service_unit.SYSTEMD_UNIT: "bot-bottle-hostctl.service",
        }
        for got, want in expected.items():
            with self.subTest(want=want):
                self.assertEqual(want, got)
        self.assertEqual("10.242.255.0/24", dgw.DEFAULT_GATEWAY_SUBNET)

    def test_service_unit_env_has_no_environment(self) -> None:
        from bot_bottle.control.hostctl import service_unit  # pylint: disable=import-outside-toplevel
        spec = service_unit.ServiceSpec(python="/py", path_env="/bin")
        self.assertEqual({"PATH", "PYTHONPATH", "BOT_BOTTLE_ROOT"}, set(spec.environment()))

    def test_firecracker_pool(self) -> None:
        pool = netpool.from_settings(InfraSettings())
        self.assertEqual(netpool.NetPool(
            pool_size=8, ip_base="10.243.0.0", iface_prefix="bbfc",
            nft_table="bot_bottle_fc", orch_iface="bborch0", gw_iface="bbgw0",
            env_slot=0, env_slot_count=4), pool)
        self.assertEqual(("bbfc0", "10.243.0.0", "10.243.0.1"),
                         (pool.slot(0).iface, pool.slot(0).host_ip, pool.slot(0).guest_ip))
        self.assertEqual(("bborch0", "10.243.255.0", "10.243.255.1"),
                         (pool.orch_slot().iface, pool.orch_slot().host_ip,
                          pool.orch_slot().guest_ip))
        self.assertEqual(("bbgw0", "10.243.255.2", "10.243.255.3"),
                         (pool.gw_slot().iface, pool.gw_slot().host_ip, pool.gw_slot().guest_ip))
        self.assertEqual("", pool.nondefault_env())

    def test_firecracker_dirs(self) -> None:
        from bot_bottle.backend.firecracker import util  # pylint: disable=import-outside-toplevel
        self.assertEqual(util.cache_dir(), util.env_dir())

    def test_docker_agent_pool_default(self) -> None:
        # pylint: disable=import-outside-toplevel
        from bot_bottle.backend.docker import agent_network
        with mock.patch.object(agent_network, "_docker_subnets", return_value=[]), \
                mock.patch.object(agent_network, "run_docker") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            self.assertEqual("10.242.0.0/29", agent_network.create_agent_network("n"))

    def test_settings_fingerprint(self) -> None:
        self.assertEqual("4955111b08908f4605de0d3788b6aeb684f6626775cf1f4a512f31d3aa496b59",
                         InfraSettings().fingerprint())


class TestNamedDerivations(unittest.TestCase):
    def test_pure_derivations(self) -> None:
        home = Path("/home/u")
        self.assertEqual(home / ".bot-bottle-envs" / "dev", env.default_root("dev", home))
        self.assertEqual("bb-dev-", env.resource_prefix("dev"))
        self.assertEqual("io.bot-bottle.dev", env.reverse_dns("dev"))
        self.assertEqual("10.240.0.0/16", env.docker_agent_subnets("dev"))

    def test_docker_gateway_subnet_is_stable_and_off_live(self) -> None:
        import ipaddress  # pylint: disable=import-outside-toplevel
        seen = set()
        for name in ("dev", "ci-1", "ci-2", "ci-6839", "x"):
            subnet = env.docker_gateway_subnet(name)
            self.assertEqual(subnet, env.docker_gateway_subnet(name))
            net = ipaddress.IPv4Network(subnet)
            self.assertEqual(24, net.prefixlen)
            self.assertTrue(net.subnet_of(ipaddress.IPv4Network("10.241.0.0/16")))
            seen.add(subnet)
        self.assertGreater(len(seen), 1)

    def test_macos_v6_is_never_lives(self) -> None:
        for name in ("dev", "ci-1", "ci-macos"):
            v6 = env.macos_agent_subnet_v6(name)
            self.assertTrue(v6.startswith("fd62:6f74:6275:"), v6)
            self.assertTrue(v6.endswith("::/64"))

    def test_module_names_in_a_named_environment(self) -> None:
        # Names are fixed at import, so check them in a fresh interpreter.
        code = (
            "from bot_bottle.control import gateway as c\n"
            "from bot_bottle.backend.docker import orchestrator as o, compose, gateway as g\n"
            "from bot_bottle.backend.macos_container import gateway as m, host_firewall as f\n"
            "from bot_bottle.control.hostctl import service_unit as u\n"
            "from bot_bottle.paths import bot_bottle_root\n"
            "import json\n"
            "print(json.dumps([c.GATEWAY_NAME, c.GATEWAY_NETWORK, o.ORCHESTRATOR_NAME,\n"
            "    compose.compose_project_name('s'), g.GATEWAY_EGRESS_NETWORK,\n"
            "    m.GATEWAY_NAME, f.ANCHOR, f.DAEMON_LABEL, u.SYSTEMD_UNIT, u.LAUNCHD_LABEL,\n"
            "    str(bot_bottle_root())]))\n"
        )
        with tempfile.TemporaryDirectory() as home:
            environ = {k: v for k, v in os.environ.items() if k != "BOT_BOTTLE_ROOT"}
            environ.update({"HOME": home, "BOT_BOTTLE_ENV": "dev", "PYTHONPATH": str(REPO)})
            out = subprocess.run([sys.executable, "-P", "-c", code], env=environ, cwd=home,
                                 capture_output=True, text=True, check=True).stdout
            self.assertEqual([
                "bb-dev-orch-gateway", "bb-dev-gateway", "bb-dev-orchestrator", "bb-dev-s",
                "bb-dev-gateway-egress", "bb-dev-mac-infra", "com.apple/io.bot-bottle.dev",
                "io.bot-bottle.dev.pf", "bb-dev-hostctl.service", "io.bot-bottle.dev.hostctl",
                str(Path(home) / ".bot-bottle-envs" / "dev"),
            ], json.loads(out))

    def test_explicit_root_wins(self) -> None:
        from bot_bottle.paths import bot_bottle_root  # pylint: disable=import-outside-toplevel
        with mock.patch.dict(os.environ, {"BOT_BOTTLE_ENV": "dev", "BOT_BOTTLE_ROOT": "/x/y"}):
            self.assertEqual(Path("/x/y"), bot_bottle_root())
        with mock.patch.dict(os.environ, {"BOT_BOTTLE_ENV": "dev"}):
            os.environ.pop("BOT_BOTTLE_ROOT", None)
            self.assertEqual(Path.home() / ".bot-bottle-envs" / "dev", bot_bottle_root())

    def test_service_unit_carries_the_environment(self) -> None:
        from bot_bottle.control.hostctl import service_unit  # pylint: disable=import-outside-toplevel
        with mock.patch.dict(os.environ, {"BOT_BOTTLE_ENV": "dev"}):
            spec = service_unit.ServiceSpec(python="/py", path_env="/bin")
            self.assertEqual("dev", spec.environment()["BOT_BOTTLE_ENV"])

    def test_firecracker_env_dir(self) -> None:
        from bot_bottle.backend.firecracker import util  # pylint: disable=import-outside-toplevel
        self.assertEqual(util.cache_dir() / "envs" / "dev", util.env_dir("dev"))


class TestRegistry(_TmpHome):
    def test_register_is_idempotent_and_keeps_the_slot(self) -> None:
        root = env.default_root("dev", self.home)
        env.register("dev", root, self.home)
        self.assertEqual(1, env.claim_slot("dev", 4, self.home))
        reg = env.register("dev", root, self.home)
        self.assertEqual(env.Registration("dev", str(root), 1), reg)
        self.assertEqual([reg], env.registered(self.home))

    def test_live_isnt_registered(self) -> None:
        with self.assertRaises(env.InvalidEnvironmentError):
            env.register("live", self.home / ".bot-bottle", self.home)
        self.assertEqual(0, env.claim_slot("live", 4, self.home))
        self.assertEqual([], env.registered(self.home))

    def test_a_name_sharing_a_prefix_is_refused(self) -> None:
        env.register("dev", env.default_root("dev", self.home), self.home)
        for clash in ("dev-2", "dev-a-b"):
            with self.subTest(clash=clash), self.assertRaises(env.InvalidEnvironmentError) as cm:
                env.register(clash, env.default_root(clash, self.home), self.home)
            self.assertIn("envs rm dev", str(cm.exception))
        env.register("dev2", env.default_root("dev2", self.home), self.home)  # no clash
        self.assertEqual("", env.prefix_clash("ci-12", ["ci-1", "ci-123"]))
        self.assertEqual("ci", env.prefix_clash("ci-1", ["ci"]))

    def test_slots_are_lowest_free_and_stable(self) -> None:
        self.assertEqual(1, env.claim_slot("a", 3, self.home))
        self.assertEqual(2, env.claim_slot("b", 3, self.home))
        self.assertEqual(1, env.claim_slot("a", 3, self.home))
        self.assertEqual(3, env.claim_slot("c", 3, self.home))
        env.unregister("a", self.home)
        self.assertEqual(1, env.claim_slot("d", 3, self.home))

    def test_exhaustion_says_how_to_raise_it(self) -> None:
        env.claim_slot("a", 1, self.home)
        with self.assertRaises(env.NoFreeSlotError) as cm:
            env.claim_slot("b", 1, self.home)
        message = str(cm.exception)
        for part in ("all 1 reserved", "1: a", "envs rm", "BOT_BOTTLE_FC_ENV_SLOTS", "envSlots",
                     "install --backend=firecracker"):
            self.assertIn(part, message)

    def test_a_slot_beyond_a_lowered_count_is_reclaimed(self) -> None:
        env.claim_slot("a", 4, self.home)
        env.claim_slot("b", 4, self.home)
        self.assertEqual(1, env.claim_slot("a", 1, self.home))
        with self.assertRaises(env.NoFreeSlotError):
            env.claim_slot("b", 1, self.home)

    def test_ensure_active_registered(self) -> None:
        with mock.patch.dict(os.environ, {"BOT_BOTTLE_ENV": "dev", "HOME": str(self.home)}):
            os.environ.pop("BOT_BOTTLE_ROOT", None)
            self.assertEqual("dev", env.ensure_active_registered())
            reg = env.read("dev")
        self.assertIsNotNone(reg)
        assert reg is not None
        self.assertEqual(str(self.home / ".bot-bottle-envs" / "dev"), reg.root)
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            os.environ.pop("BOT_BOTTLE_ENV", None)
            self.assertEqual("live", env.ensure_active_registered())
        self.assertEqual(["dev"], [r.name for r in env.registered(self.home)])

    def test_unreadable_registrations_are_skipped(self) -> None:
        (self.home / ".bot-bottle-envs" / "junk").mkdir(parents=True)
        (self.home / ".bot-bottle-envs" / "junk" / "env.json").write_text("{nope")
        (self.home / ".bot-bottle-envs" / "other").mkdir()
        (self.home / ".bot-bottle-envs" / "other" / "env.json").write_text(
            json.dumps({"name": "different", "root": "/r"}))
        self.assertEqual([], env.registered(self.home))
        self.assertEqual([], env.registered(self.home / "missing"))


class TestFirecrackerSlices(_TmpHome):
    def test_slice_math(self) -> None:
        self.assertEqual(netpool.EnvSlice(0, "10.243.0.0", "bbfc", "bot_bottle_fc",
                                          "bborch0", "bbgw0"), netpool.env_slice(0))
        self.assertEqual(netpool.EnvSlice(1, "10.243.1.0", "bb1fc", "bot_bottle_fc1",
                                          "bborch1", "bbgw1"), netpool.env_slice(1))
        self.assertEqual(netpool.EnvSlice(63, "10.243.63.0", "bb63fc", "bot_bottle_fc63",
                                          "bborch63", "bbgw63"), netpool.env_slice(63))
        for bad in (-1, 64):
            with self.subTest(slot=bad), self.assertRaises(ValueError):
                netpool.env_slice(bad)
        with self.assertRaises(ValueError):
            netpool.env_slice(1, pool_size=129)
        netpool.env_slice(0, pool_size=200)  # live isn't bound by a slice's /24

    def test_infra_links_move_up_four_per_slice(self) -> None:
        for k in (0, 1, 2, 63):
            sl = netpool.env_slice(k)
            pool = netpool.NetPool(8, sl.ip_base, sl.iface_prefix, sl.nft_table,
                                   sl.orch_iface, sl.gw_iface, env_slot=k)
            self.assertEqual(f"10.243.255.{4 * k}", pool.orch_slot().host_ip)
            self.assertEqual(f"10.243.255.{4 * k + 1}", pool.orch_slot().guest_ip)
            self.assertEqual(f"10.243.255.{4 * k + 2}", pool.gw_slot().host_ip)
            self.assertEqual(f"10.243.255.{4 * k + 3}", pool.gw_slot().guest_ip)

    def test_slices_never_overlap_or_share_a_wildcard(self) -> None:
        pools = []
        for k in range(0, 64):
            sl = netpool.env_slice(k)
            pools.append(netpool.NetPool(8, sl.ip_base, sl.iface_prefix, sl.nft_table,
                                         sl.orch_iface, sl.gw_iface, env_slot=k))
        addresses: set[str] = set()
        for pool in pools:
            mine = {a for s in [*pool.all_slots(), pool.orch_slot(), pool.gw_slot()]
                    for a in (s.host_ip, s.guest_ip)}
            self.assertFalse(addresses & mine)
            addresses |= mine
        for a in pools:
            for b in pools:
                if a is not b:
                    self.assertFalse(any(s.iface.startswith(a.iface_prefix)
                                         for s in b.all_slots()))
                    self.assertNotEqual(a.nft_table, b.nft_table)

    def test_a_named_environment_claims_its_slice(self) -> None:
        with mock.patch.object(netpool, "claim_slot", return_value=2) as claim:
            pool = netpool.from_settings(InfraSettings(), env="dev")
        claim.assert_called_once_with("dev", 4)
        self.assertEqual(("10.243.2.0", "bb2fc", "bot_bottle_fc2", "bborch2", "bbgw2", 2),
                         (pool.ip_base, pool.iface_prefix, pool.nft_table, pool.orch_iface,
                          pool.gw_iface, pool.env_slot))
        self.assertEqual("10.243.255.8", pool.orch_slot().host_ip)

    def test_explicit_settings_win(self) -> None:
        with mock.patch.object(netpool, "claim_slot", return_value=2):
            pool = netpool.from_settings(InfraSettings(fc_ip_base="10.9.0.0",
                                                       fc_iface_prefix="bbx",
                                                       fc_env_slots=7), env="dev")
        self.assertEqual(("10.9.0.0", "bbx", "bot_bottle_fc2", 7),
                         (pool.ip_base, pool.iface_prefix, pool.nft_table, pool.env_slot_count))

    def test_no_free_slice_dies_clearly(self) -> None:
        err = io.StringIO()
        with mock.patch.object(netpool, "claim_slot",
                               side_effect=env.NoFreeSlotError("all 4 reserved slots are held")), \
                contextlib.redirect_stderr(err), self.assertRaises(Die):
            netpool.from_settings(InfraSettings(), env="dev")
        self.assertIn("all 4 reserved slots are held", err.getvalue())

    def test_the_host_pool_is_lives_from_any_environment(self) -> None:
        with mock.patch.dict(os.environ, {"BOT_BOTTLE_ENV": "dev"}), \
                mock.patch.object(netpool, "claim_slot") as claim:
            pool = netpool.host_pool(InfraSettings(fc_ip_base="10.9.0.0", fc_env_slots=6))
        claim.assert_not_called()
        self.assertEqual(("10.243.0.0", "bbfc", 0, 6),
                         (pool.ip_base, pool.iface_prefix, pool.env_slot, pool.env_slot_count))
        self.assertIn("BOT_BOTTLE_FC_ENV_SLOTS=6", pool.nondefault_env())
        self.assertIn("Environment=BOT_BOTTLE_FC_ENV_SLOTS=6",
                      netpool.render_systemd_unit(pool, "u", "/s"))

    def test_the_setup_script_agrees_with_python(self) -> None:
        script = (REPO / "scripts" / "firecracker-netpool.sh").read_text()
        body = script[:script.rindex('case "${1:-}" in')]
        probe = "\n".join(
            f'_select_slice {k}; echo "$IP_BASE $PREFIX $TABLE $ORCH_IFACE $GW_IFACE '
            f'$(orch_host) $(orch_guest) $(gw_host) $(gw_guest) $(host_ip 7) $(guest_ip 7)"'
            for k in (0, 1, 2, 4, 63))
        # Every value from the shared defaults file, as the NixOS unit passes them.
        environ = {k: v for k, v in os.environ.items() if not k.startswith("BOT_BOTTLE_FC_")}
        for line in netpool.DEFAULTS_FILE.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                key, _, value = line.partition("=")
                environ[key] = value
        environ["BOT_BOTTLE_FC_ENV_SLOTS"] = "63"
        path = self.home / "netpool-probe.sh"
        path.write_text(body + "\n" + probe + "\n")
        out = subprocess.run(["bash", str(path)], env=environ, capture_output=True, text=True,
                             check=True).stdout.splitlines()
        self.assertEqual(5, len(out))
        for line, k in zip(out, (0, 1, 2, 4, 63)):
            sl = netpool.env_slice(k)
            pool = netpool.NetPool(8, sl.ip_base, sl.iface_prefix, sl.nft_table,
                                   sl.orch_iface, sl.gw_iface, env_slot=k)
            want = [sl.ip_base, sl.iface_prefix, sl.nft_table, sl.orch_iface, sl.gw_iface,
                    pool.orch_slot().host_ip, pool.orch_slot().guest_ip,
                    pool.gw_slot().host_ip, pool.gw_slot().guest_ip,
                    pool.slot(7).host_ip, pool.slot(7).guest_ip]
            with self.subTest(slot=k):
                self.assertEqual(want, line.split())

    def test_the_script_refuses_too_many_slices(self) -> None:
        environ = dict(os.environ, BOT_BOTTLE_FC_ENV_SLOTS="64")
        proc = subprocess.run(["bash", str(REPO / "scripts" / "firecracker-netpool.sh"),
                               "status"], env=environ, capture_output=True, text=True,
                              check=False)
        self.assertEqual(1, proc.returncode)
        self.assertIn("at most 63", proc.stderr)


class TestMacosAnchors(_TmpHome):
    def test_other_environments_subnets(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.backend.macos_container import host_firewall as hf
        rules = self.home / "anchors"
        rules.mkdir()
        (rules / "io.bot-bottle").write_text(hf.render_rules("192.168.128.0/24", "fd::/64"))
        (rules / "io.bot-bottle.dev").write_text(hf.render_rules("192.168.200.0/24", "fd::/64"))
        (rules / "io.bot-bottle.pf.plist").write_text("not rules")
        (rules / "com.apple").write_text(hf.render_rules("10.0.0.0/24", "fd::/64"))
        with mock.patch.object(hf, "RULES_PATH", rules / "io.bot-bottle.dev"):
            self.assertEqual({"192.168.128.0/24"}, hf.other_environments_subnets(rules))
        with mock.patch.object(hf, "RULES_PATH", rules / "io.bot-bottle"):
            self.assertEqual({"192.168.200.0/24"}, hf.other_environments_subnets(rules))
        self.assertEqual(set(), hf.other_environments_subnets(self.home / "missing"))

    def test_choose_subnet_skips_them(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.backend.macos_container import host_firewall as hf
        with mock.patch.object(hf, "installed_subnet", return_value=None), \
                mock.patch.object(hf, "other_environments_subnets",
                                  return_value={hf.CANDIDATE_SUBNETS[0]}), \
                mock.patch.object(hf, "conflicts", return_value=[]):
            self.assertEqual((hf.CANDIDATE_SUBNETS[1], []), hf.choose_subnet())


class _Probe:
    def __init__(self, hostctl: set[str], containers: list[str], fc: set[str]) -> None:
        self.hostctl, self.containers_, self.fc = hostctl, containers, fc

    def make(self):  # type: ignore[no-untyped-def]
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.commands.envs import Probe
        return Probe(hostctl=lambda root: str(root) in self.hostctl,
                     containers=lambda: self.containers_,
                     firecracker_infra=lambda name: name in self.fc)


class TestEnvsList(_TmpHome):
    def test_lists_live_then_registered_with_what_runs(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.commands.envs import EnvRow, collect
        dev_root = env.default_root("dev", self.home)
        env.register("dev", dev_root, self.home)
        env.claim_slot("dev", 4, self.home)
        env.register("ci-7", env.default_root("ci-7", self.home), self.home)
        probe = _Probe(hostctl={str(self.home / ".bot-bottle")},
                       containers=["bot-bottle-orchestrator", "bot-bottle-s1", "bb-dev-gateway",
                                   "unrelated", "bb-ci-70-x"],
                       fc={"dev"}).make()
        rows = collect(probe, home=self.home, environ={"BOT_BOTTLE_ENV": "dev"})
        self.assertEqual([
            EnvRow("live", str(self.home / ".bot-bottle"), 0, False, True, ("2 containers",)),
            EnvRow("ci-7", str(self.home / ".bot-bottle-envs" / "ci-7"), 0, False, False, ()),
            EnvRow("dev", str(dev_root), 1, True, False, ("1 container", "firecracker VMs")),
        ], rows)

    def test_lives_root_follows_an_override_only_when_live_is_active(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.commands.envs import collect
        probe = _Probe(set(), [], set()).make()
        rows = collect(probe, home=self.home, environ={"BOT_BOTTLE_ROOT": "/custom"})
        self.assertEqual("/custom", rows[0].root)
        self.assertTrue(rows[0].active)
        rows = collect(probe, home=self.home,
                       environ={"BOT_BOTTLE_ROOT": "/custom", "BOT_BOTTLE_ENV": "dev"})
        self.assertEqual(str(self.home / ".bot-bottle"), rows[0].root)

    def test_cmd_prints_a_table(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.commands import envs
        out = io.StringIO()
        with mock.patch.object(envs, "collect", return_value=[
                envs.EnvRow("live", "/r", 0, True, True, ())]), contextlib.redirect_stdout(out):
            self.assertEqual(0, envs.cmd_envs([]))
        self.assertEqual("*\tlive\t/r\t0\trunning\t-\n", out.getvalue())

    def test_unknown_subcommand(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.commands import envs
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, envs.cmd_envs(["frob"]))


class TestEnvsRm(_TmpHome):
    def _plan(self, name: str, **kw):  # type: ignore[no-untyped-def]
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.commands.envs import plan_rm
        return plan_rm(name, home=self.home, python="/py", environ={"PATH": "/bin"}, **kw)

    def test_live_and_bad_names_are_refused(self) -> None:
        for name in ("live", "Bad"):
            with self.subTest(name=name), self.assertRaises(env.InvalidEnvironmentError):
                self._plan(name)

    def test_plan_for_a_default_root(self) -> None:
        root = env.default_root("dev", self.home)
        env.register("dev", root, self.home)
        plan = self._plan("dev")
        self.assertEqual(root, plan.root)
        self.assertEqual(("/py", "-P", "-m", "bot_bottle.cli", "envs", "_teardown"),
                         plan.teardown)
        self.assertEqual({"PATH": "/bin", "BOT_BOTTLE_ENV": "dev", "BOT_BOTTLE_ROOT": str(root),
                          "PYTHONPATH": str(REPO)}, plan.env)
        self.assertEqual((root, self.home / ".cache" / "bot-bottle" / "firecracker" / "envs"
                          / "dev"), plan.remove)
        self.assertEqual((), plan.kept)

    def test_the_teardown_runs_this_package(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.commands.envs import plan_rm
        plan = plan_rm("dev", home=self.home, python=sys.executable,
                       environ={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": "/extra",
                                "HOME": str(self.home)})
        self.assertEqual(os.pathsep.join([str(REPO), "/extra"]), plan.env["PYTHONPATH"])
        # From an unrelated directory, `-P` and all: the hidden subcommand is
        # reached and refuses to tear down without being in the environment.
        env_live = dict(plan.env)
        del env_live["BOT_BOTTLE_ENV"]
        proc = subprocess.run(list(plan.teardown), env=env_live, cwd=self.home,
                              capture_output=True, text=True, check=False)
        self.assertEqual(2, proc.returncode, proc.stderr)
        self.assertIn("refusing to tear down live", proc.stderr)

    def test_an_unregistered_name_still_plans_its_default_dirs(self) -> None:
        plan = self._plan("ci-9")
        self.assertEqual(self.home / ".bot-bottle-envs" / "ci-9", plan.root)

    def test_an_explicit_root_is_kept(self) -> None:
        other = self.home / "elsewhere"
        env.register("dev", other, self.home)
        plan = self._plan("dev")
        self.assertEqual(other, plan.root)
        self.assertNotIn(other, plan.remove)
        self.assertIn("outside", plan.kept[0])

    def test_a_root_pointing_at_live_is_kept(self) -> None:
        live = self.home / ".bot-bottle"
        env.register("dev", live, self.home)
        plan = self._plan("dev")
        self.assertNotIn(live, plan.remove)
        self.assertIn("live install", plan.kept[0])

    def _execute(self, plan, code: int, force: bool = False) -> tuple[int, list[object]]:  # type: ignore[no-untyped-def]
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.commands.envs import execute_rm
        calls: list[object] = []

        def run(argv, **kw):  # type: ignore[no-untyped-def]
            calls.append((argv, kw["env"]))
            return subprocess.CompletedProcess(argv, code)

        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return execute_rm(plan, force=force, run=run), calls

    def _registered_dev(self):  # type: ignore[no-untyped-def]
        root = env.default_root("dev", self.home)
        env.register("dev", root, self.home)
        env.claim_slot("dev", 4, self.home)
        (root / "db").mkdir(parents=True)
        fc = self.home / ".cache" / "bot-bottle" / "firecracker" / "envs" / "dev"
        fc.mkdir(parents=True)
        return root, fc

    def test_a_clean_teardown_deletes_state_and_frees_the_slot(self) -> None:
        root, fc = self._registered_dev()
        code, calls = self._execute(self._plan("dev"), 0)
        self.assertEqual(0, code)
        self.assertEqual(1, len(calls))
        self.assertFalse(root.exists())
        self.assertFalse(fc.exists())
        self.assertIsNone(env.read("dev", self.home))
        self.assertEqual(1, env.claim_slot("other", 4, self.home))

    def test_a_failed_teardown_keeps_state(self) -> None:
        root, fc = self._registered_dev()
        code, _ = self._execute(self._plan("dev"), 1)
        self.assertEqual(1, code)
        self.assertTrue(root.exists())
        self.assertTrue(fc.exists())
        self.assertIsNotNone(env.read("dev", self.home))

    def test_force_deletes_state_after_a_failed_teardown(self) -> None:
        root, _ = self._registered_dev()
        code, _ = self._execute(self._plan("dev"), 1, force=True)
        self.assertEqual(1, code)
        self.assertFalse(root.exists())
        self.assertIsNone(env.read("dev", self.home))

    def test_cmd_refuses_live(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.commands import envs
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(2, envs.cmd_envs(["rm", "live"]))
        self.assertIn("uninstall", err.getvalue())

    def test_cmd_dry_run_changes_nothing(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.commands import envs
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), \
                mock.patch.object(envs, "execute_rm") as execute, contextlib.redirect_stdout(out):
            os.environ.pop("BOT_BOTTLE_ROOT", None)
            self.assertEqual(0, envs.cmd_envs(["rm", "dev", "--dry-run"]))
        execute.assert_not_called()
        self.assertIn("tear down dev", out.getvalue())
        self.assertIn(f"delete {self.home / '.bot-bottle-envs' / 'dev'}", out.getvalue())

    def test_teardown_refuses_live(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.commands import envs
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, envs.cmd_envs(["_teardown"]))


class TestTeardown(unittest.TestCase):
    """`envs _teardown` inside a named environment, with the backends mocked."""

    def _run(self, *, available: dict[str, bool], held: list[str], infra_error: bool = False,
             resource_ok: bool = True) -> tuple[int, dict[str, mock.MagicMock], str]:
        # pylint: disable=import-outside-toplevel
        from bot_bottle.backend.host_resources import HostResource
        from bot_bottle.cli.commands import envs
        backends: dict[str, mock.MagicMock] = {}

        def get(settings, name, prompt=True):  # type: ignore[no-untyped-def]
            del settings, prompt
            b = mock.MagicMock(name=name)
            b.prepare_cleanup.return_value.empty = False
            if infra_error:
                b.infra_service.return_value.stop.side_effect = RuntimeError("boom")
            removed = mock.MagicMock(return_value=resource_ok)
            image = mock.MagicMock(return_value=True)
            b.host_resources.return_value = [HostResource("network bb-dev-x", removed),
                                             HostResource("image bot-bottle-claude", image)]
            b.removed, b.image = removed, image
            backends[name] = b
            return b

        def has(settings: object, name: str) -> bool:
            del settings
            return available[name]

        err = io.StringIO()
        state = object() if held else None
        with mock.patch.dict(os.environ, {"BOT_BOTTLE_ENV": "dev"}), \
                mock.patch("bot_bottle.backend.has_backend", has), \
                mock.patch("bot_bottle.backend.known_backend_names",
                           return_value=tuple(available)), \
                mock.patch("bot_bottle.backend.get_bottle_backend", get), \
                mock.patch("bot_bottle.control.hostctl.runner.running", return_value=state), \
                mock.patch("bot_bottle.control.hostctl.runner.held_bottles", return_value=held), \
                mock.patch("bot_bottle.control.hostctl.runner.stop") as stop, \
                mock.patch("bot_bottle.control.hostctl.service_unit.uninstall") as uninstall, \
                mock.patch("bot_bottle.cli.commands.cleanup._teardown_held",
                           return_value=[]) as teardown_held, \
                contextlib.redirect_stderr(err):
            code = envs.cmd_envs(["_teardown"])
        stop.assert_called_once_with(force=True)
        uninstall.assert_called_once_with()
        if held:
            teardown_held.assert_called_once_with(held)
        return code, backends, err.getvalue()

    def test_tears_down_every_available_backend_but_keeps_images(self) -> None:
        code, backends, _ = self._run(available={"docker": True, "firecracker": True,
                                                 "macos-container": False}, held=["b1"])
        self.assertEqual(0, code)
        self.assertEqual({"docker", "firecracker"}, set(backends))
        for b in backends.values():
            b.cleanup.assert_called_once()
            b.infra_service.return_value.stop.assert_called_once_with()
            b.removed.assert_called_once_with()
            b.image.assert_not_called()

    def test_reports_failures(self) -> None:
        code, _, err = self._run(available={"docker": True}, held=[], infra_error=True,
                                 resource_ok=False)
        self.assertEqual(1, code)
        self.assertIn("docker orchestrator and gateway: boom", err)
        self.assertIn("couldn't remove network bb-dev-x", err)


class TestUninstallInANamedEnvironment(unittest.TestCase):
    def test_refused(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.commands.uninstall import cmd_uninstall
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"BOT_BOTTLE_ENV": "dev"}), \
                mock.patch("bot_bottle.cli.commands.uninstall._backends") as backends, \
                contextlib.redirect_stderr(err):
            self.assertEqual(2, cmd_uninstall(["--yes"]))
        backends.assert_not_called()
        self.assertIn("envs rm dev", err.getvalue())


class TestEnvironmentShown(unittest.TestCase):
    def test_doctor_line(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.commands.doctor import environment_line
        with mock.patch.dict(os.environ, {"BOT_BOTTLE_ENV": "dev", "BOT_BOTTLE_ROOT": "/r"}):
            self.assertEqual("dev (BOT_BOTTLE_ENV=dev; state in /r)", environment_line())
        with mock.patch.dict(os.environ, {"BOT_BOTTLE_ROOT": "/r"}):
            os.environ.pop("BOT_BOTTLE_ENV", None)
            self.assertEqual("live (the default; state in /r)", environment_line())


if __name__ == "__main__":
    unittest.main()
