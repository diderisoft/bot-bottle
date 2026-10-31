"""Unit: named environments (`BOT_BOTTLE_ENV`): validation, what each
environment derives, live's values unchanged, host-wide slice claims, the
firecracker slice math (Python and the setup script agree), docker and macOS
subnet allocation, and `envs`
listing and removal planning with the host mocked out."""

# pylint: disable=reimported,too-many-return-statements,too-many-lines
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
# Imported here, with no BOT_BOTTLE_ENV set, so the live names they fix at
# import are the ones every test sees, whatever a test patches later.
from bot_bottle.backend.docker import agent_network as _agent_network
from bot_bottle.backend.docker import gateway as _docker_gateway
from bot_bottle.backend.firecracker import netpool, slice_claims
from bot_bottle.backend.macos_container import host_firewall as _host_firewall
from bot_bottle.backend.macos_container import enumerate as _menum
from bot_bottle.backend.macos_container import orchestrator as _morch
from bot_bottle.backend.docker import compose as _compose
from bot_bottle.backend.docker import orchestrator as _dorch
from bot_bottle.backend.firecracker import gateway as _fgw, orchestrator as _forch
from bot_bottle.control import gateway as _cgw
from bot_bottle.control.hostctl import service_unit as _service_unit
from bot_bottle.contract.hostctl import InfraSettings
from bot_bottle.lib.log import Die

_IMPORTED_AS_LIVE = (_agent_network, _docker_gateway, _host_firewall, _menum, _morch, _compose,
                     _dorch, _fgw, _forch, _cgw, _service_unit)

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

    def test_docker_orchestrator_publishes_8099(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.contract.hostctl import Settings
        orch = _dorch.DockerOrchestrator(settings=Settings())
        self.assertEqual(8099, orch.port)
        self.assertEqual("http://127.0.0.1:8099", orch.url())

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

    def test_reattach_finds_only_this_environments_bottles(self) -> None:
        # The gateway reattach (and cleanup, enumeration) find bottles by
        # compose project prefix, fixed at import: in `dev` that's `bb-dev-`,
        # so a live `bot-bottle-*` project is neither found nor touched.
        code = (
            "import json, subprocess\n"
            "from unittest import mock\n"
            "from bot_bottle.backend.docker import compose, gateway_attach\n"
            "ls = json.dumps([{'Name': 'bot-bottle-live1'}, {'Name': 'bb-dev-mine'},\n"
            "                 {'Name': 'unrelated'}])\n"
            "done = subprocess.CompletedProcess([], 0, ls, '')\n"
            "with mock.patch.object(compose.subprocess, 'run', return_value=done):\n"
            "    print(json.dumps([compose.list_active_slugs(), compose.compose_project_name('s'),\n"
            "                      gateway_attach.DockerGatewayAttacher()._gateway]))\n"
        )
        with tempfile.TemporaryDirectory() as home:
            environ = {k: v for k, v in os.environ.items() if k != "BOT_BOTTLE_ROOT"}
            environ.update({"HOME": home, "BOT_BOTTLE_ENV": "dev", "PYTHONPATH": str(REPO)})
            out = subprocess.run([sys.executable, "-P", "-c", code], env=environ, cwd=home,
                                 capture_output=True, text=True, check=True).stdout
        self.assertEqual([["mine"], "bb-dev-s", "bb-dev-orch-gateway"], json.loads(out))

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
    def test_register_is_idempotent(self) -> None:
        root = env.default_root("dev", self.home)
        reg = env.register("dev", root, self.home)
        self.assertEqual(env.Registration("dev", str(root)), reg)
        self.assertEqual(reg, env.register("dev", root, self.home))
        self.assertEqual([reg], env.registered(self.home))
        self.assertEqual(root / "env.json", env.registration_file("dev", self.home))

    def test_live_isnt_registered(self) -> None:
        with self.assertRaises(env.InvalidEnvironmentError):
            env.register("live", self.home / ".bot-bottle", self.home)
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


class TestSliceClaims(_TmpHome):
    """Host-wide claims: one directory, users simulated by uid and home."""

    def setUp(self) -> None:
        super().setUp()
        self.dir = self.home / "slices"
        self.dir.mkdir()
        self.busy: set[int] = set()

    def _home(self, user: str) -> Path:
        home = self.home / user
        home.mkdir(exist_ok=True)
        return home

    def _claim(self, user: str, uid: int, name: str, count: int = 3) -> int:
        home = self._home(user)
        env.register(name, env.default_root(name, home), home)
        return slice_claims.claim(self.dir, env=name, count=count, uid=uid, user=user,
                                  registration=env.registration_file(name, home),
                                  in_use=lambda k: k in self.busy)

    def test_two_users_never_share_a_slice(self) -> None:
        self.assertEqual(1, self._claim("alice", 1000, "dev"))
        self.assertEqual(2, self._claim("runner", 1001, "ci-7"))
        # The same name for another user is another environment.
        self.assertEqual(3, self._claim("runner", 1001, "dev"))
        self.assertEqual(1, self._claim("alice", 1000, "dev"))
        claims = slice_claims.read_all(self.dir)
        self.assertEqual({1: ("dev", 1000), 2: ("ci-7", 1001), 3: ("dev", 1001)},
                         {k: (c.env, c.uid) for k, c in claims.items() if c})
        self.assertEqual("0o664", oct((self.dir / "1").stat().st_mode & 0o777))

    def test_exhaustion_names_the_holders_and_how_to_raise_it(self) -> None:
        self._claim("alice", 1000, "dev", count=1)
        with self.assertRaises(slice_claims.NoFreeSliceError) as cm:
            self._claim("runner", 1001, "ci-7", count=1)
        message = str(cm.exception)
        for part in ("all 1 reserved", "1: alice:dev", "envs rm",
                     "BOT_BOTTLE_FC_ENV_SLOTS", "envSlots", "install --backend=firecracker"):
            self.assertIn(part, message)

    def test_release_frees_only_the_owners_claim(self) -> None:
        self._claim("alice", 1000, "dev")
        self.assertFalse(slice_claims.release(self.dir, env="dev", uid=1001))
        self.assertTrue(slice_claims.release(self.dir, env="dev", uid=1000))
        self.assertFalse(slice_claims.release(self.dir, env="dev", uid=1000))
        self.assertEqual(1, self._claim("runner", 1001, "ci-7"))

    def test_a_stale_claim_is_replaced(self) -> None:
        self._claim("alice", 1000, "dev")
        # alice's registration is gone (her env was deleted by hand).
        (self._home("alice") / ".bot-bottle-envs" / "dev" / "env.json").unlink()
        self.assertEqual(1, self._claim("runner", 1001, "ci-7"))

    def test_a_live_claim_is_never_stolen(self) -> None:
        self._claim("alice", 1000, "dev")
        (self._home("alice") / ".bot-bottle-envs" / "dev" / "env.json").unlink()
        self.busy.add(1)  # but its VMs still hold the TAPs
        self.assertEqual(2, self._claim("runner", 1001, "ci-7"))

    def test_an_unreadable_registration_counts_as_present(self) -> None:
        self.assertFalse(slice_claims.registration_gone(""))
        with mock.patch("os.stat", side_effect=PermissionError):
            self.assertFalse(slice_claims.registration_gone("/home/other/x"))
        self.assertTrue(slice_claims.registration_gone(str(self.home / "nope")))

    def test_an_unreadable_claim_is_held(self) -> None:
        (self.dir / "1").write_text("{garbage")
        self.assertEqual(2, self._claim("alice", 1000, "dev"))

    def test_concurrent_claims_get_distinct_slices(self) -> None:
        import threading  # pylint: disable=import-outside-toplevel
        got: list[int] = []
        lock = threading.Lock()

        def worker(i: int) -> None:
            slot = self._claim(f"user{i}", 2000 + i, f"env{i}", count=8)
            with lock:
                got.append(slot)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(list(range(1, 9)), sorted(got))

    def test_concurrent_claims_across_processes(self) -> None:
        code = (
            "import sys; from pathlib import Path\n"
            "from bot_bottle.backend.firecracker import slice_claims as c\n"
            "print(c.claim(Path(sys.argv[1]), env=sys.argv[2], count=6, uid=int(sys.argv[3]),"
            " user='u', registration=Path(sys.argv[4]), in_use=lambda k: False))\n")
        reg = self.home / "reg"
        reg.write_text("{}")
        procs = [subprocess.Popen(  # pylint: disable=consider-using-with
            [sys.executable, "-c", code, str(self.dir), f"e{i}", str(3000 + i), str(reg)],
            env=dict(os.environ, PYTHONPATH=str(REPO)), stdout=subprocess.PIPE, text=True)
            for i in range(6)]
        slots = sorted(int(p.communicate()[0]) for p in procs)
        self.assertEqual([1, 2, 3, 4, 5, 6], slots)

    def test_a_missing_directory_says_to_rerun_the_setup(self) -> None:
        with self.assertRaises(slice_claims.SliceClaimError) as cm:
            slice_claims.claim(self.home / "absent", env="dev", count=1, uid=1, user="u",
                               registration=self.home / "r", in_use=lambda k: False)
        self.assertIn("install --backend=firecracker", str(cm.exception))

    def test_netpool_claims_in_the_configured_directory(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.backend import environment_slice, release_environment_slice
        from bot_bottle.cli.settings import settings_from_env  # pylint: disable=import-outside-toplevel
        settings = settings_from_env({"BOT_BOTTLE_FC_SLICES_DIR": str(self.dir)})
        self.assertEqual(str(self.dir), settings.infra.fc_slices_dir)
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            pool = netpool.from_settings(settings.infra, env="dev")
            self.assertEqual(1, pool.env_slot)
            self.assertEqual(1, environment_slice(settings, "dev"))
            self.assertTrue(release_environment_slice(settings, "dev"))
            self.assertIsNone(environment_slice(settings, "dev"))

    def test_slice_in_use_reads_tap_carrier(self) -> None:
        with mock.patch.object(netpool, "_carrier", side_effect="bbgw2".__eq__):
            self.assertTrue(netpool.slice_in_use(2))
            self.assertFalse(netpool.slice_in_use(1))


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
        with mock.patch.object(netpool, "claim_slice", return_value=2) as claim:
            pool = netpool.from_settings(InfraSettings(), env="dev")
        claim.assert_called_once_with(InfraSettings(), "dev", 4)
        self.assertEqual(("10.243.2.0", "bb2fc", "bot_bottle_fc2", "bborch2", "bbgw2", 2),
                         (pool.ip_base, pool.iface_prefix, pool.nft_table, pool.orch_iface,
                          pool.gw_iface, pool.env_slot))
        self.assertEqual("10.243.255.8", pool.orch_slot().host_ip)

    def test_explicit_settings_win(self) -> None:
        with mock.patch.object(netpool, "claim_slice", return_value=2):
            pool = netpool.from_settings(InfraSettings(fc_ip_base="10.9.0.0",
                                                       fc_iface_prefix="bbx",
                                                       fc_env_slots=7), env="dev")
        self.assertEqual(("10.9.0.0", "bbx", "bot_bottle_fc2", 7),
                         (pool.ip_base, pool.iface_prefix, pool.nft_table, pool.env_slot_count))

    def test_no_free_slice_dies_clearly(self) -> None:
        err = io.StringIO()
        with mock.patch.object(netpool, "claim_slice", side_effect=slice_claims.NoFreeSliceError(
                "all 4 reserved slices are held")), \
                contextlib.redirect_stderr(err), self.assertRaises(Die):
            netpool.from_settings(InfraSettings(), env="dev")
        self.assertIn("all 4 reserved slices are held", err.getvalue())

    def test_the_host_pool_is_lives_from_any_environment(self) -> None:
        with mock.patch.dict(os.environ, {"BOT_BOTTLE_ENV": "dev"}), \
                mock.patch.object(netpool, "claim_slice") as claim:
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


class _FakeDocker:
    """`docker network` with a set of subnets other environments hold."""

    def __init__(self, listed: set[str], racing: set[str] | None = None,
                 existing: dict[str, str] | None = None) -> None:
        # Subnets of networks `docker network ls` shows; ones another
        # environment takes between our listing and our create.
        self.listed, self.racing = set(listed), set(racing or ())
        self.networks = dict(existing or {})  # name -> subnet
        self.creates: list[str] = []

    def __call__(self, argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        def done(code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, code, out, err)
        if argv[:3] == ["docker", "network", "ls"]:
            return done(0, " ".join(f"id-{s}" for s in [*self.listed, *self.networks.values()]))
        if argv[:3] == ["docker", "network", "inspect"] and argv[-1].startswith("id-"):
            return done(0, " ".join(a[3:] for a in argv[5:]))
        if argv[:3] == ["docker", "network", "inspect"]:
            name = argv[-1]
            if name not in self.networks:
                return done(1, err=f"Error: No such network: {name}")
            return done(0, f"{self.networks[name]}|true")
        if argv[:3] == ["docker", "network", "create"]:
            subnet = argv[argv.index("--subnet") + 1]
            self.creates.append(subnet)
            if subnet in self.listed | self.racing:
                return done(1, err="Error response from daemon: Pool overlaps with other one "
                                   "on this address space")
            self.networks[argv[-1]] = subnet
            return done(0, "netid")
        return done(0)


class TestDockerGatewayAllocation(_TmpHome):
    def _gateway(self, fake: _FakeDocker):  # type: ignore[no-untyped-def]
        # pylint: disable=import-outside-toplevel
        from bot_bottle.backend.docker import agent_network, gateway
        from bot_bottle.contract.hostctl import Settings
        patches = [mock.patch.object(gateway, "run_docker", fake),
                   mock.patch.object(agent_network, "run_docker", fake)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return gateway.DockerGateway(settings=Settings(), network="bb-dev-gateway")

    def _env(self) -> "contextlib.AbstractContextManager[object]":
        return mock.patch.dict(os.environ, {"BOT_BOTTLE_ENV": "dev",
                                            "BOT_BOTTLE_ROOT": str(self.home / "root")})

    def test_an_overlap_moves_to_the_next_free_24_and_records_it(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.backend.docker import gateway
        first, second, third = list(env.docker_gateway_candidates("dev"))[:3]
        # `first` only reports an overlap (another environment raced us to
        # it); `second` is a network docker lists, so it isn't even tried.
        fake = _FakeDocker(listed={second}, racing={first})
        with self._env():
            gw = self._gateway(fake)
            self.assertEqual(first, gw._subnet)  # pylint: disable=protected-access
            gw._ensure_network()  # pylint: disable=protected-access
            self.assertEqual(third, gw._subnet)  # pylint: disable=protected-access
            self.assertEqual(third, gateway.recorded_gateway_subnet())
            # Restarted: the recorded subnet is used, with no new create.
            again = self._gateway(fake)
            self.assertEqual(third, again._subnet)  # pylint: disable=protected-access
            again._ensure_network()  # pylint: disable=protected-access
        self.assertEqual(third, fake.networks["bb-dev-gateway"])
        self.assertNotIn(second, fake.creates)  # skipped: docker already listed it
        self.assertEqual([first, third], fake.creates)

    def test_an_existing_network_is_adopted_and_recorded(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.backend.docker import gateway
        fake = _FakeDocker(listed=set(), existing={"bb-dev-gateway": "10.241.9.0/24"})
        with self._env():
            gw = self._gateway(fake)
            gw._ensure_network()  # pylint: disable=protected-access
            self.assertEqual("10.241.9.0/24", gw._subnet)  # pylint: disable=protected-access
            self.assertEqual("10.241.9.0/24", gateway.recorded_gateway_subnet())
        self.assertEqual([], fake.creates)

    def test_live_and_explicit_subnets_never_allocate(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.contract.hostctl import Settings
        fake = _FakeDocker(listed=set(), racing={"10.242.255.0/24"})
        gw = self._gateway(fake)
        with self.assertRaises(Exception) as cm:
            gw._ensure_network()  # pylint: disable=protected-access
        self.assertIn("Pool overlaps", str(cm.exception))
        self.assertEqual(["10.242.255.0/24"], fake.creates)
        with self._env():
            # pylint: disable-next=import-outside-toplevel
            from bot_bottle.backend.docker import gateway
            explicit = gateway.DockerGateway(settings=Settings(), network="n",
                                             subnet="10.9.9.0/24")
            self.assertFalse(explicit._allocate)  # pylint: disable=protected-access

    def test_every_candidate_taken_says_how_to_choose(self) -> None:
        fake = _FakeDocker(listed=set(env.docker_gateway_candidates("dev")))
        with self._env():
            gw = self._gateway(fake)
            with self.assertRaises(Exception) as cm:
                gw._ensure_network()  # pylint: disable=protected-access
        self.assertIn("BOT_BOTTLE_DOCKER_GATEWAY_SUBNET", str(cm.exception))
        self.assertEqual(0, len(fake.creates))  # docker listed them all as taken


class _PortDocker:
    """`docker run` that reports some host ports as already allocated."""

    def __init__(self, taken: set[int]) -> None:
        self.taken = taken
        self.published: list[str] = []
        self.removed = 0

    def __call__(self, argv: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["docker", "run"]:
            spec = argv[argv.index("--publish") + 1]
            self.published.append(spec)
            if int(spec.split(":")[1]) in self.taken:
                return subprocess.CompletedProcess(
                    argv, 125, "", "docker: Error response from daemon: failed to set up "
                    "container networking: driver failed programming external connectivity "
                    f"on endpoint x: Bind for {spec.rsplit(':', 1)[0]} failed: port is "
                    "already allocated")
        if argv[:3] == ["docker", "rm", "--force"]:
            self.removed += 1
        if argv[:3] == ["docker", "network", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, "false", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class TestDockerOrchestratorPorts(_TmpHome):
    def _orch(self, fake: _PortDocker, **kw: object):  # type: ignore[no-untyped-def]
        # pylint: disable=import-outside-toplevel
        from bot_bottle.backend.docker import orchestrator
        from bot_bottle.contract.hostctl import Settings
        for p in (mock.patch.object(orchestrator, "run_docker", fake),
                  mock.patch("bot_bottle.trust_domain.host_signing_key", return_value="k")):
            p.start()
            self.addCleanup(p.stop)
        return orchestrator.DockerOrchestrator(settings=Settings(), **kw)  # type: ignore[arg-type]

    def _env(self) -> "contextlib.AbstractContextManager[object]":
        return mock.patch.dict(os.environ, {"BOT_BOTTLE_ENV": "ci-6865",
                                            "BOT_BOTTLE_ROOT": str(self.home / "root")})

    def test_a_taken_port_moves_to_the_next_and_is_recorded(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.backend.docker import orchestrator
        first, second, third = list(env.docker_orchestrator_port_candidates("ci-6865"))[:3]
        fake = _PortDocker(taken={first, second})
        with self._env():
            orch = self._orch(fake)
            self.assertEqual(first, orch.port)
            orch._run_container("hash")  # pylint: disable=protected-access
            self.assertEqual(third, orch.port)
            self.assertEqual(f"http://127.0.0.1:{third}", orch.url())
            self.assertEqual(third, orchestrator.recorded_orchestrator_port(orch.name))
            # Every other process of the environment finds the same port.
            self.assertEqual(third, self._orch(fake).port)
        self.assertEqual([f"127.0.0.1:{p}:8099" for p in (first, second, third)],
                         fake.published)
        self.assertNotIn("127.0.0.1:8099:8099", fake.published)
        self.assertTrue(all(18100 <= p < 18900 for p in (first, second, third)))

    def test_a_second_pair_keeps_its_own_port(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.backend.docker import orchestrator
        fake = _PortDocker(taken=set())
        with self._env():
            main = self._orch(fake)
            main._run_container("h")  # pylint: disable=protected-access
            fake.taken.add(main.port)
            other = self._orch(fake, name="bb-ci-6865-orchestrator-itest")
            other._run_container("h")  # pylint: disable=protected-access
            self.assertNotEqual(main.port, other.port)
            self.assertEqual(main.port, orchestrator.recorded_orchestrator_port(main.name))

    def test_live_and_explicit_ports_never_move(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.control.lifecycle import OrchestratorStartError
        fake = _PortDocker(taken={8099, 20001})
        live = self._orch(fake)
        self.assertEqual(8099, live.port)
        with self.assertRaises(OrchestratorStartError) as cm:
            live._run_container("h")  # pylint: disable=protected-access
        self.assertIn("port is already allocated", str(cm.exception))
        with self._env():
            explicit = self._orch(fake, port=20001)
            with self.assertRaises(OrchestratorStartError):
                explicit._run_container("h")  # pylint: disable=protected-access
        self.assertEqual(["127.0.0.1:8099:8099", "127.0.0.1:20001:8099"], fake.published)

    def test_another_failure_is_not_retried(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.control.lifecycle import OrchestratorStartError

        class Broken(_PortDocker):
            def __call__(self, argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
                if argv[:2] == ["docker", "run"]:
                    self.published.append(argv[argv.index("--publish") + 1])
                    return subprocess.CompletedProcess(argv, 125, "", "no such image")
                return super().__call__(argv, **kw)

        fake = Broken(taken=set())
        with self._env(), self.assertRaises(OrchestratorStartError):
            self._orch(fake)._run_container("h")  # pylint: disable=protected-access
        self.assertEqual(1, len(fake.published))

    def test_the_client_never_probes_lives_port_from_a_named_environment(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.control import client
        from bot_bottle.contract.hostctl import Settings  # pylint: disable=import-outside-toplevel
        with self._env(), mock.patch.object(netpool, "from_settings",
                                            side_effect=RuntimeError("no fc")):
            urls, _ = client._candidates(Settings())  # pylint: disable=protected-access
            self.assertFalse(any(":8099" in u for u in urls))
            # pylint: disable-next=import-outside-toplevel
            from bot_bottle.backend.docker import orchestrator
            orchestrator.record_orchestrator_port(18123)
            urls, _ = client._candidates(Settings())  # pylint: disable=protected-access
            self.assertIn("http://127.0.0.1:18123", urls)


class TestMacosV6Allocation(_TmpHome):
    def test_skips_other_environments_and_keeps_the_installed_one(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.backend.macos_container import host_firewall as hf
        rules = self.home / "anchors"
        rules.mkdir()
        first, second = list(env.macos_agent_subnet_v6_candidates("dev"))[:2]
        (rules / "io.bot-bottle.ci-macos").write_text(hf.render_rules("10.213.128.0/24", first))
        mine = rules / "io.bot-bottle.dev"
        with mock.patch.dict(os.environ, {"BOT_BOTTLE_ENV": "dev"}), \
                mock.patch.object(hf, "RULES_PATH", mine):
            self.assertEqual({first}, hf.other_environments_subnets_v6(rules))
            self.assertEqual(second, hf.choose_subnet_v6(rules))
            self.assertEqual(first, hf.agent_subnet_v6())  # nothing recorded yet
            mine.write_text(hf.render_rules("192.168.200.0/24", second))
            self.assertEqual(second, hf.installed_subnet_v6())
            self.assertEqual(second, hf.agent_subnet_v6())
            self.assertEqual(second, hf.choose_subnet_v6(rules))

    def test_live_keeps_its_fixed_64(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.backend.macos_container import host_firewall as hf
        with mock.patch.object(hf, "RULES_PATH", self.home / "none"):
            self.assertEqual("fd62:6f74:6274:6c65::/64", hf.choose_subnet_v6(self.home))
            self.assertEqual("fd62:6f74:6274:6c65::/64", hf.agent_subnet_v6())


class _Probe:
    def __init__(self, hostctl: set[str], containers: list[str], fc: set[str],
                 slices: dict[str, int] | None = None) -> None:
        self.hostctl, self.containers_, self.fc = hostctl, containers, fc
        self.slices = slices or {}

    def make(self):  # type: ignore[no-untyped-def]
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.commands.envs import Probe
        return Probe(hostctl=lambda root: str(root) in self.hostctl,
                     containers=lambda: self.containers_,
                     firecracker_infra=lambda name: name in self.fc,
                     slice=self.slices.get)


class TestEnvsList(_TmpHome):
    def test_lists_live_then_registered_with_what_runs(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.commands.envs import EnvRow, collect
        dev_root = env.default_root("dev", self.home)
        env.register("dev", dev_root, self.home)
        env.register("ci-7", env.default_root("ci-7", self.home), self.home)
        probe = _Probe(hostctl={str(self.home / ".bot-bottle")},
                       containers=["bot-bottle-orchestrator", "bot-bottle-s1", "bb-dev-gateway",
                                   "unrelated", "bb-ci-70-x"],
                       fc={"dev"}, slices={"dev": 1}).make()
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
        self.released: list[str] = []  # pylint: disable=attribute-defined-outside-init
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.commands.envs import execute_rm
        calls: list[object] = []

        def run(argv, **kw):  # type: ignore[no-untyped-def]
            calls.append((argv, kw["env"]))
            return subprocess.CompletedProcess(argv, code)

        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return execute_rm(plan, force=force, run=run,
                              release=lambda n: self.released.append(n) is None), calls

    def _registered_dev(self):  # type: ignore[no-untyped-def]
        root = env.default_root("dev", self.home)
        env.register("dev", root, self.home)
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
        self.assertEqual(["dev"], self.released)

    def test_a_failed_teardown_keeps_state(self) -> None:
        root, fc = self._registered_dev()
        code, _ = self._execute(self._plan("dev"), 1)
        self.assertEqual(1, code)
        self.assertTrue(root.exists())
        self.assertTrue(fc.exists())
        self.assertIsNotNone(env.read("dev", self.home))
        self.assertEqual([], self.released)

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
