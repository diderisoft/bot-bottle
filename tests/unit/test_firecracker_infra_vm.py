"""Unit tests for the Firecracker infra VMs (orchestrator + gateway boot).

The KVM boot / HTTP reachability is integration-tested on a KVM host; here we
cover the URL shape, the shared-rootfs role wiring, the secret-push decisions,
and the two-VM singleton lifecycle that must hold without a VM.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.backend.firecracker import infra_vm


class TestPushSecrets(unittest.TestCase):
    """`_push_signing_key` (orchestrator) and `_push_gateway_jwt` (gateway)
    seed their guest's secret over SSH via `_push_secret`. The host token file
    stays the single source of truth, never clobbered per-backend (#469); the
    gateway gets a pre-minted `gateway` JWT, never the signing key."""

    def _infra(self) -> infra_vm.InfraVm:
        return infra_vm.InfraVm(guest_ip="10.0.0.1", private_key=Path("/k"))

    def test_signing_key_written_atomically(self):
        proc = MagicMock(returncode=0, stderr="")
        with patch.object(infra_vm, "host_orchestrator_token", return_value="host-key"), \
                patch.object(infra_vm.subprocess, "run", return_value=proc) as run:
            infra_vm._push_signing_key(self._infra())
        # Piped the key to an atomic write of the guest token path.
        self.assertEqual("host-key", run.call_args.kwargs["input"])
        remote_cmd = run.call_args.args[0][-1]
        self.assertIn(f"cat > {infra_vm._GUEST_SIGNING_KEY_PATH}.tmp", remote_cmd)
        self.assertIn(f"mv {infra_vm._GUEST_SIGNING_KEY_PATH}.tmp "
                      f"{infra_vm._GUEST_SIGNING_KEY_PATH}", remote_cmd)

    def test_gateway_jwt_is_the_pre_minted_token_not_the_key(self):
        # The host mints the `gateway` JWT and hands it in; `_push_gateway_jwt`
        # only writes it. It is the JWT (never the key itself) that lands in the
        # gateway VM.
        proc = MagicMock(returncode=0, stderr="")
        with patch.object(infra_vm.subprocess, "run", return_value=proc) as run:
            infra_vm._push_gateway_jwt(self._infra(), "gw.jwt")
        self.assertEqual("gw.jwt", run.call_args.kwargs["input"])
        remote_cmd = run.call_args.args[0][-1]
        self.assertIn(f"cat > {infra_vm._GUEST_GATEWAY_JWT_PATH}.tmp", remote_cmd)

    def test_dies_when_push_never_succeeds(self):
        proc = MagicMock(returncode=255, stderr="ssh: connect refused")
        with patch.object(infra_vm, "host_orchestrator_token", return_value="host-key"), \
                patch.object(infra_vm, "_SECRET_PUSH_TIMEOUT_SECONDS", 0.05), \
                patch.object(infra_vm, "_SECRET_PUSH_POLL_SECONDS", 0.0), \
                patch.object(infra_vm.subprocess, "run", return_value=proc), \
                patch.object(infra_vm, "die", side_effect=SystemExit) as die:
            with self.assertRaises(SystemExit):
                infra_vm._push_signing_key(self._infra())
        die.assert_called_once()


class TestOrchestratorUrl(unittest.TestCase):
    def test_url_uses_guest_ip_and_port(self):
        infra = infra_vm.InfraVm(
            vm=MagicMock(), guest_ip="10.243.255.1", private_key=Path("/k"))
        self.assertEqual(
            f"http://10.243.255.1:{infra_vm.ORCHESTRATOR_PORT}",
            infra.orchestrator_url,
        )


class TestInfraEndpoint(unittest.TestCase):
    """The endpoint mirrors docker/macOS: it exposes the orchestrator's URL and
    delegates the CA fetch to the gateway VM handle."""

    def _endpoint(self) -> infra_vm.InfraEndpoint:
        return infra_vm.InfraEndpoint(
            orchestrator=infra_vm.InfraVm(guest_ip="10.243.255.1", private_key=Path("/k")),
            gateway=infra_vm.InfraVm(guest_ip="10.243.255.3", private_key=Path("/k")),
        )

    def test_orchestrator_url_is_the_orchestrator_vm(self):
        ep = self._endpoint()
        self.assertEqual(
            f"http://10.243.255.1:{infra_vm.ORCHESTRATOR_PORT}", ep.orchestrator_url)

    def test_gateway_ca_pem_delegates_to_the_gateway_vm(self):
        ep = self._endpoint()
        with patch.object(ep.gateway, "gateway_ca_pem", return_value="PEM") as ca:
            self.assertEqual("PEM", ep.gateway_ca_pem(timeout=1))
        ca.assert_called_once_with(timeout=1)


class TestBuildInfraRootfs(unittest.TestCase):
    def test_uses_infra_variant_and_role_branched_init(self):
        with patch.object(infra_vm.util, "build_base_rootfs_dir") as build:
            build.return_value = Path("/cache/rootfs/x-infra")
            infra_vm.build_infra_rootfs_dir()
        build.assert_called_once()
        self.assertEqual(infra_vm._INFRA_IMAGE, build.call_args.args[0])
        # variant is "-infra-<init-hash>" so an init change rebuilds the rootfs.
        self.assertTrue(build.call_args.kwargs["variant"].startswith("-infra-"))
        init = build.call_args.kwargs["init_script"]
        # One shared init, role-branched off the kernel cmdline: it starts the
        # control plane OR the gateway data plane, and exports PATH so the
        # gateway daemons' subprocesses find python3.
        self.assertIn("bb_role=", init)
        self.assertIn('if [ "$ROLE" = gateway ]; then', init)
        self.assertIn("bot_bottle.orchestrator", init)
        self.assertIn("bot_bottle.gateway.bootstrap", init)
        self.assertIn("export PATH=", init)
        # Persistent registry volume mounted at the DB dir on the orchestrator.
        self.assertIn("/dev/vdb", init)
        # VM backend uses git-http (9420); the git:// daemon is left out.
        self.assertIn("BOT_BOTTLE_GATEWAY_DAEMONS=egress,git-http,supervise", init)
        # Role-scoped auth (#469): the orchestrator gets the host-seeded signing
        # key; the gateway gets the host-minted `gateway` JWT (NOT the key — the
        # JWT is minted on the host now, so the init never touches the key to
        # mint it); each plane refuses to start without its secret.
        self.assertIn(f"cat {infra_vm._GUEST_SIGNING_KEY_PATH}", init)  # host-seeded key
        self.assertIn(f"cat {infra_vm._GUEST_GATEWAY_JWT_PATH}", init)  # host-minted JWT
        self.assertNotIn("ROLE_GATEWAY", init)  # minting moved to the host
        self.assertIn("refusing to start the control plane", init)     # no open mode
        self.assertIn("refusing to start the data plane", init)        # gateway fail-closed
        self.assertIn('BOT_BOTTLE_ORCHESTRATOR_TOKEN="$CP_KEY" python3 -m bot_bottle.orchestrator',
                      init)  # key -> orchestrator only
        self.assertIn('BOT_BOTTLE_ORCHESTRATOR_AUTH_JWT="$GW_JWT"', init)  # JWT -> gateway daemons
        # The gateway resolves the orchestrator's address off the cmdline
        # (bb_orch), so no IP is baked into the artifact.
        self.assertIn("bb_orch=", init)
        self.assertIn("BOT_BOTTLE_ORCHESTRATOR_URL=http://$ORCH:", init)


class TestBootRole(unittest.TestCase):
    """`boot_orchestrator` / `boot_gateway` boot the shared rootfs with the
    right `bb_role` cmdline, memory ceiling, and (orchestrator-only) registry
    volume; each seeds its own secret."""

    def _boot_env(self):
        vm = MagicMock()
        vm.process.pid = 4242
        return vm

    def test_orchestrator_role_mem_and_registry(self):
        import tempfile
        vm = self._boot_env()
        with tempfile.TemporaryDirectory() as td, \
                patch.object(infra_vm.netpool, "tap_present", return_value=True), \
                patch.object(infra_vm.infra_artifact, "local_build_requested", return_value=False), \
                patch.object(infra_vm.infra_artifact, "materialize_ext4"), \
                patch.object(infra_vm.infra_artifact, "infra_artifact_version", return_value="v"), \
                patch.object(infra_vm, "_stable_keypair", return_value=(Path("/k"), "pub")), \
                patch.object(infra_vm, "_ensure_registry_volume", return_value=Path("/reg")), \
                patch.object(infra_vm, "_push_signing_key") as push, \
                patch.object(infra_vm.firecracker_vm, "boot", return_value=vm) as boot, \
                patch.object(infra_vm, "_orch_dir", return_value=Path(td)):
            infra_vm.boot_orchestrator()
        kw = boot.call_args.kwargs
        self.assertIn("bb_role=orchestrator", kw["extra_boot_args"])
        self.assertEqual(infra_vm._ORCH_MEM_MIB, kw["mem_mib"])
        self.assertEqual(Path("/reg"), kw["data_drive"])  # DB volume on the CP
        push.assert_called_once()  # signing key seeded

    def test_gateway_role_carries_orch_addr_and_no_registry(self):
        import tempfile
        vm = self._boot_env()
        with tempfile.TemporaryDirectory() as td, \
                patch.object(infra_vm.netpool, "tap_present", return_value=True), \
                patch.object(infra_vm.infra_artifact, "local_build_requested", return_value=False), \
                patch.object(infra_vm.infra_artifact, "materialize_ext4"), \
                patch.object(infra_vm.infra_artifact, "infra_artifact_version", return_value="v"), \
                patch.object(infra_vm, "_stable_keypair", return_value=(Path("/k"), "pub")), \
                patch.object(infra_vm, "_push_gateway_jwt") as push, \
                patch.object(infra_vm.firecracker_vm, "boot", return_value=vm) as boot, \
                patch.object(infra_vm, "_gw_dir", return_value=Path(td)):
            infra_vm.boot_gateway("10.243.255.1", "gw.jwt")
        kw = boot.call_args.kwargs
        self.assertIn("bb_role=gateway", kw["extra_boot_args"])
        self.assertIn("bb_orch=10.243.255.1", kw["extra_boot_args"])  # reaches the CP
        self.assertEqual(infra_vm._GW_MEM_MIB, kw["mem_mib"])
        self.assertIsNone(kw["data_drive"])  # data plane never opens the DB
        # The host-minted token is threaded through to the JWT push.
        self.assertEqual("gw.jwt", push.call_args.args[1])


class TestSshGatewayTransport(unittest.TestCase):
    def test_cp_into_preserves_source_mode(self):
        import tempfile
        from subprocess import CompletedProcess
        with tempfile.NamedTemporaryFile() as f:
            os.chmod(f.name, 0o700)  # like the staged access-hook
            t = infra_vm.SshGatewayTransport(Path("/k"), "10.0.0.1")
            with patch.object(infra_vm.subprocess, "run",
                              return_value=CompletedProcess([], 0)) as run:
                t.cp_into(f.name, "/etc/git-gate/access-hook")
        remote_cmd = run.call_args.args[0][-1]
        self.assertIn("chmod 700", remote_cmd)  # exec bit preserved over SSH

    def test_exec_raises_on_failure(self):
        from subprocess import CompletedProcess
        t = infra_vm.SshGatewayTransport(Path("/k"), "10.0.0.1")
        with patch.object(infra_vm.subprocess, "run",
                          return_value=CompletedProcess([], 1, stderr="nope")), \
             self.assertRaises(infra_vm.GatewayProvisionError):
            t.exec(["mkdir", "-p", "/git-gate"])


class TestGatewayTransportTargetsGatewayVm(unittest.TestCase):
    def test_transport_uses_the_gateway_link_guest_ip(self):
        # git-gate provisioning goes to the gateway VM (gw_slot), not the
        # orchestrator, now that the planes are split.
        t = infra_vm.gateway_transport()
        self.assertEqual(infra_vm.netpool.gw_slot().guest_ip, t._ip)


class TestGatewayCaPem(unittest.TestCase):
    def test_dies_when_cert_never_appears(self) -> None:
        from subprocess import CompletedProcess
        gw = infra_vm.InfraVm(vm=None, guest_ip="10.0.0.1", private_key=Path("/k"))
        with patch.object(infra_vm.subprocess, "run",
                          return_value=CompletedProcess([], 1, stdout="", stderr="")), \
             self.assertRaises(SystemExit):
            gw.gateway_ca_pem(timeout=0)


class TestRegistryVolume(unittest.TestCase):
    def test_reuses_existing_volume(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            vol = Path(td) / "registry.ext4"
            vol.write_bytes(b"")  # already present
            with patch.object(infra_vm, "registry_volume_path", return_value=vol), \
                 patch.object(infra_vm.subprocess, "run") as run:
                out = infra_vm._ensure_registry_volume()
            run.assert_not_called()  # no mke2fs when it exists
            self.assertEqual(vol, out)

    def test_creates_volume_when_missing(self):
        import tempfile
        from subprocess import CompletedProcess
        with tempfile.TemporaryDirectory() as td:
            vol = Path(td) / "registry.ext4"
            with patch.object(infra_vm, "registry_volume_path", return_value=vol), \
                 patch.object(infra_vm.subprocess, "run",
                              return_value=CompletedProcess([], 0)) as run:
                infra_vm._ensure_registry_volume()
            argv = run.call_args.args[0]
            self.assertIn("mke2fs", argv)
            self.assertIn(str(vol), argv)


class TestEnsureBuilt(unittest.TestCase):
    def test_default_pulls_artifact_without_docker(self):
        # PRD 0069 Stage 2: the launch host pulls the prebuilt rootfs; no Docker.
        # Pin BOT_BOTTLE_INFRA_BUILD off: the coverage CI job exports it =local
        # for the integration suite, and that ambient value would otherwise send
        # this default-path test down the local Docker-build branch.
        with patch.dict(os.environ, {"BOT_BOTTLE_INFRA_BUILD": ""}), \
                patch.object(infra_vm.docker_mod, "build_image") as build, \
                patch.object(infra_vm.infra_artifact, "ensure_artifact_gz") as pull:
            infra_vm.ensure_built()
        build.assert_not_called()
        pull.assert_called_once()

    def test_local_mode_builds_deps_before_infra(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_INFRA_BUILD": "local"}), \
                patch.object(infra_vm.docker_mod, "build_image") as build:
            infra_vm.ensure_built()
        tags = [c.args[0] for c in build.call_args_list]
        # infra is FROM gateway and COPY --from orchestrator, so both first.
        self.assertEqual(infra_vm._INFRA_IMAGE, tags[-1])
        self.assertIn(infra_vm._ORCHESTRATOR_IMAGE, tags[:-1])
        self.assertIn(infra_vm._GATEWAY_IMAGE, tags[:-1])


class TestWaitForHealth(unittest.TestCase):
    def _infra(self, alive: bool = True) -> infra_vm.InfraVm:
        vm = MagicMock()
        vm.is_alive.return_value = alive
        return infra_vm.InfraVm(vm=vm, guest_ip="10.0.0.1", private_key=Path("/k"))

    def test_returns_on_200(self):
        infra = self._infra()
        cm = MagicMock()
        cm.__enter__.return_value.status = 200
        with patch.object(infra_vm.urllib.request, "urlopen", return_value=cm):
            infra_vm.wait_for_health(infra, timeout=5)  # must not raise

    def test_dies_when_vm_exits(self):
        infra = self._infra(alive=False)
        assert infra.vm is not None  # narrow for the type checker (it's a mock)
        infra.vm.process.returncode = 1
        with patch.object(infra_vm.firecracker_vm, "_console_tail", return_value=""), \
             self.assertRaises(SystemExit):
            infra_vm.wait_for_health(infra, timeout=5)


class TestEnsureRunningSingleton(unittest.TestCase):
    def test_adopts_when_healthy_alive_and_version_matches(self):
        # Healthy control plane + live gateway + existing key + matching version
        # marker -> adopt (no boot); both VM handles have vm=None.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "id_ed25519").write_text("k")
            (d / "booted-version").write_text("v-current\n")
            with patch.object(infra_vm, "_infra_dir", return_value=d), \
                 patch.object(infra_vm, "_expected_version", return_value="v-current"), \
                 patch.object(infra_vm, "_health_ok", return_value=True), \
                 patch.object(infra_vm, "_pidfile_alive", return_value=True), \
                 patch.object(infra_vm, "boot_orchestrator") as boot_o, \
                 patch.object(infra_vm, "boot_gateway") as boot_g:
                ep = infra_vm.ensure_running()
        boot_o.assert_not_called()
        boot_g.assert_not_called()
        self.assertIsNone(ep.orchestrator.vm)
        self.assertIsNone(ep.gateway.vm)
        # The two handles address the two distinct infra links.
        self.assertEqual(infra_vm.netpool.orch_slot().guest_ip, ep.orchestrator.guest_ip)
        self.assertEqual(infra_vm.netpool.gw_slot().guest_ip, ep.gateway.guest_ip)

    def test_reboots_both_when_version_stale(self):
        # Healthy control plane but the running pair booted an OLDER image
        # (marker mismatch) -> reboot rather than adopt stale code.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "id_ed25519").write_text("k")
            (d / "booted-version").write_text("v-old\n")
            with patch.object(infra_vm, "_infra_dir", return_value=d), \
                 patch.object(infra_vm, "_expected_version", return_value="v-current"), \
                 patch.object(infra_vm, "_health_ok", return_value=True), \
                 patch.object(infra_vm, "_pidfile_alive", return_value=True), \
                 patch.object(infra_vm, "stop") as stop, \
                 patch.object(infra_vm, "ensure_built"), \
                 patch.object(infra_vm, "wait_for_health"), \
                 patch.object(infra_vm, "host_orchestrator_token", return_value="k"), \
                 patch.object(infra_vm, "mint", return_value="gw.jwt"), \
                 patch.object(infra_vm, "boot_orchestrator") as boot_o, \
                 patch.object(infra_vm, "boot_gateway") as boot_g:
                boot_o.return_value = infra_vm.InfraVm(
                    guest_ip="10.243.255.1", private_key=Path("/k"), vm=MagicMock())
                boot_g.return_value = infra_vm.InfraVm(
                    guest_ip="10.243.255.3", private_key=Path("/k"), vm=MagicMock())
                infra_vm.ensure_running()
            stop.assert_called_once()          # dislodge the outdated pair
            boot_o.assert_called_once()
            boot_g.assert_called_once()
            # The gateway is booted pointing at the orchestrator's guest IP,
            # carrying the host-minted `gateway` token.
            self.assertEqual("10.243.255.1", boot_g.call_args.args[0])
            self.assertEqual("gw.jwt", boot_g.call_args.args[1])
            # The fresh boot records the current version for the next launcher.
            self.assertEqual("v-current\n", (d / "booted-version").read_text())

    def test_reboots_when_gateway_dead(self):
        # Orchestrator healthy + marker current, but the gateway VM is gone ->
        # the pair is half-down, so reboot both rather than adopt partial state.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "id_ed25519").write_text("k")
            (d / "booted-version").write_text("v-current\n")
            with patch.object(infra_vm, "_infra_dir", return_value=d), \
                 patch.object(infra_vm, "_expected_version", return_value="v-current"), \
                 patch.object(infra_vm, "_health_ok", return_value=True), \
                 patch.object(infra_vm, "_pidfile_alive", return_value=False), \
                 patch.object(infra_vm, "stop") as stop, \
                 patch.object(infra_vm, "ensure_built"), \
                 patch.object(infra_vm, "wait_for_health"), \
                 patch.object(infra_vm, "host_orchestrator_token", return_value="k"), \
                 patch.object(infra_vm, "mint", return_value="gw.jwt"), \
                 patch.object(infra_vm, "boot_orchestrator") as boot_o, \
                 patch.object(infra_vm, "boot_gateway") as boot_g:
                boot_o.return_value = infra_vm.InfraVm(
                    guest_ip="10.243.255.1", private_key=Path("/k"), vm=MagicMock())
                boot_g.return_value = infra_vm.InfraVm(
                    guest_ip="10.243.255.3", private_key=Path("/k"), vm=MagicMock())
                infra_vm.ensure_running()
            stop.assert_called_once()
            boot_o.assert_called_once()
            boot_g.assert_called_once()

    def test_boots_both_when_unhealthy(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with patch.object(infra_vm, "_infra_dir", return_value=Path(td)), \
                 patch.object(infra_vm, "_expected_version", return_value="v-current"), \
                 patch.object(infra_vm, "_health_ok", return_value=False), \
                 patch.object(infra_vm, "stop") as stop, \
                 patch.object(infra_vm, "ensure_built") as built, \
                 patch.object(infra_vm, "host_orchestrator_token", return_value="k"), \
                 patch.object(infra_vm, "mint", return_value="gw.jwt"), \
                 patch.object(infra_vm, "boot_orchestrator") as boot_o, \
                 patch.object(infra_vm, "boot_gateway") as boot_g, \
                 patch.object(infra_vm, "wait_for_health") as wait:
                boot_o.return_value = infra_vm.InfraVm(
                    guest_ip="10.243.255.1", private_key=Path("/k"), vm=MagicMock())
                boot_g.return_value = infra_vm.InfraVm(
                    guest_ip="10.243.255.3", private_key=Path("/k"), vm=MagicMock())
                infra_vm.ensure_running()
        stop.assert_called_once()      # clear stale VMs first
        built.assert_called_once()
        # Orchestrator boots + becomes healthy BEFORE the gateway (its daemons
        # reach the control plane at startup).
        boot_o.assert_called_once()
        wait.assert_called_once()
        boot_g.assert_called_once()


class TestStop(unittest.TestCase):
    def test_stops_both_vms_and_drops_marker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "booted-version").write_text("v\n")
            with patch.object(infra_vm, "_infra_dir", return_value=d), \
                 patch.object(infra_vm, "_orch_dir", return_value=d / "orchestrator"), \
                 patch.object(infra_vm, "_gw_dir", return_value=d / "gateway"), \
                 patch.object(infra_vm, "_kill_pidfile") as kill, \
                 patch.object(infra_vm, "_kill_infra_firecrackers"):
                infra_vm.stop()
            # Both VMs' pidfiles are reaped, and the version marker is gone so a
            # stopped pair is never adopted.
            self.assertEqual(2, kill.call_count)
            self.assertFalse((d / "booted-version").exists())


class TestKillPidfile(unittest.TestCase):
    def test_noop_when_no_pidfile(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with patch.object(infra_vm.os, "kill") as kill:
                infra_vm._kill_pidfile(Path(td))  # no vm.pid -> must not raise
            kill.assert_not_called()

    def test_skips_dead_or_recycled_pid(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "vm.pid").write_text("999999")  # not a live firecracker
            with patch.object(infra_vm.os, "kill") as kill:
                infra_vm._kill_pidfile(Path(td))
            kill.assert_not_called()


class TestPidfileAlive(unittest.TestCase):
    def test_false_when_no_pidfile(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(infra_vm._pidfile_alive(Path(td)))

    def test_false_when_pid_not_firecracker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "vm.pid").write_text("999999")
            self.assertFalse(infra_vm._pidfile_alive(Path(td)))


class TestAdoptable(unittest.TestCase):
    def _dir(self, td: str, *, key: bool = True, version: str | None = None) -> Path:
        d = Path(td)
        if key:
            (d / "id_ed25519").write_text("k")
        if version is not None:
            (d / "booted-version").write_text(version + "\n")
        return d

    def test_true_when_key_version_health_and_gateway_alive(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = self._dir(td, version="v1")
            with patch.object(infra_vm, "_infra_dir", return_value=d), \
                 patch.object(infra_vm, "_health_ok", return_value=True), \
                 patch.object(infra_vm, "_pidfile_alive", return_value=True):
                self.assertTrue(infra_vm._adoptable(d / "id_ed25519", "u", "v1"))

    def test_false_when_key_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = self._dir(td, key=False, version="v1")
            with patch.object(infra_vm, "_infra_dir", return_value=d):
                self.assertFalse(infra_vm._adoptable(d / "id_ed25519", "u", "v1"))

    def test_false_when_no_version_marker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = self._dir(td)  # key present, no booted-version
            with patch.object(infra_vm, "_infra_dir", return_value=d):
                self.assertFalse(infra_vm._adoptable(d / "id_ed25519", "u", "v1"))

    def test_false_when_version_mismatch(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = self._dir(td, version="v-old")
            with patch.object(infra_vm, "_infra_dir", return_value=d), \
                 patch.object(infra_vm, "_health_ok", return_value=True), \
                 patch.object(infra_vm, "_pidfile_alive", return_value=True):
                self.assertFalse(infra_vm._adoptable(d / "id_ed25519", "u", "v1"))

    def test_false_when_gateway_dead(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = self._dir(td, version="v1")
            with patch.object(infra_vm, "_infra_dir", return_value=d), \
                 patch.object(infra_vm, "_health_ok", return_value=True), \
                 patch.object(infra_vm, "_pidfile_alive", return_value=False):
                self.assertFalse(infra_vm._adoptable(d / "id_ed25519", "u", "v1"))


class TestKillInfraFirecrackers(unittest.TestCase):
    def _fake_proc(self, root: Path, pid: int, comm: str, cmdline: list[str]) -> None:
        p = root / str(pid)
        p.mkdir()
        (p / "comm").write_text(comm + "\n")
        (p / "cmdline").write_bytes(b"\0".join(a.encode() for a in cmdline) + b"\0")

    def test_kills_both_infra_firecrackers_only(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, \
             tempfile.TemporaryDirectory() as proc:
            infra_dir = Path(td)
            orch_cfg = str(infra_dir / "orchestrator" / "config.json")
            gw_cfg = str(infra_dir / "gateway" / "config.json")
            root = Path(proc)
            # both infra VMs -> killed
            self._fake_proc(root, 111, "firecracker",
                            ["firecracker", "--no-api", "--config-file", orch_cfg])
            self._fake_proc(root, 112, "firecracker",
                            ["firecracker", "--no-api", "--config-file", gw_cfg])
            # a firecracker for a different (interactive) VM -> spared
            self._fake_proc(root, 222, "firecracker",
                            ["firecracker", "--config-file", "/home/u/other.json"])
            # a non-firecracker process on an infra config path -> spared
            self._fake_proc(root, 333, "python3", ["python3", orch_cfg])
            (root / "not-a-pid").mkdir()
            with patch.object(infra_vm, "_orch_dir", return_value=infra_dir / "orchestrator"), \
                 patch.object(infra_vm, "_gw_dir", return_value=infra_dir / "gateway"), \
                 patch.object(infra_vm.os, "kill") as kill:
                infra_vm._kill_infra_firecrackers(proc_root=root)
            killed = {c.args[0] for c in kill.call_args_list}
            self.assertEqual({111, 112}, killed)


if __name__ == "__main__":
    unittest.main()
