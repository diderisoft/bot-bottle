"""Unit tests for the Firecracker infra VM (control-plane VM boot).

The KVM boot / HTTP reachability is integration-tested on a KVM host; here
we cover the URL shape, the rootfs-variant wiring, and the health-poll
decisions that must hold without a VM.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.backend.firecracker import infra_vm


class TestSigningKeySync(unittest.TestCase):
    """`_with_signing_key` mirrors the VM's control-plane signing key into the
    host token file so the CLI signs `cli` tokens the VM verifies (issue #469)."""

    def _infra(self) -> infra_vm.InfraVm:
        return infra_vm.InfraVm(guest_ip="10.0.0.1", private_key=Path("/k"))

    def test_mirrors_guest_key_to_host_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            proc = MagicMock(returncode=0, stdout="the-signing-key\n")
            with patch.object(infra_vm.subprocess, "run", return_value=proc) as run, \
                    patch.object(infra_vm, "bot_bottle_root", return_value=root):
                out = infra_vm._with_signing_key(self._infra())
            self.assertIsInstance(out, infra_vm.InfraVm)  # returned for chaining
            token = root / infra_vm.CONTROL_PLANE_TOKEN_FILENAME
            self.assertEqual("the-signing-key", token.read_text())
            self.assertEqual(0o600, token.stat().st_mode & 0o777)
            # It cat'd the guest volume path over SSH.
            self.assertIn(infra_vm._GUEST_SIGNING_KEY_PATH, run.call_args.args[0][-1])

    def test_unreadable_key_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            proc = MagicMock(returncode=255, stdout="")
            with patch.object(infra_vm.subprocess, "run", return_value=proc), \
                    patch.object(infra_vm, "bot_bottle_root", return_value=root):
                infra_vm._with_signing_key(self._infra())  # no raise
            self.assertFalse((root / infra_vm.CONTROL_PLANE_TOKEN_FILENAME).exists())


class TestControlPlaneUrl(unittest.TestCase):
    def test_url_uses_guest_ip_and_port(self):
        infra = infra_vm.InfraVm(
            vm=MagicMock(), guest_ip="10.243.255.1", private_key=Path("/k"))
        self.assertEqual(
            f"http://10.243.255.1:{infra_vm.CONTROL_PLANE_PORT}",
            infra.control_plane_url,
        )


class TestBuildInfraRootfs(unittest.TestCase):
    def test_uses_infra_variant_and_init(self):
        with patch.object(infra_vm.util, "build_base_rootfs_dir") as build:
            build.return_value = Path("/cache/rootfs/x-infra")
            infra_vm.build_infra_rootfs_dir()
        build.assert_called_once()
        self.assertEqual(infra_vm._INFRA_IMAGE, build.call_args.args[0])
        # variant is "-infra-<init-hash>" so an init change rebuilds the rootfs.
        self.assertTrue(build.call_args.kwargs["variant"].startswith("-infra-"))
        # The init runs BOTH the control plane and the gateway data plane,
        # and exports PATH so gateway_init's subprocess daemons find python3.
        init = build.call_args.kwargs["init_script"]
        self.assertIn("bot_bottle.orchestrator", init)
        # Gateway launches via the installed package (there is no
        # /app/gateway_init.py file since the daemons moved into bot_bottle).
        self.assertIn("bot_bottle.gateway.gateway_init", init)
        self.assertIn("export PATH=", init)
        # Persistent registry volume mounted at the DB dir before the CP starts.
        self.assertIn("/dev/vdb", init)
        # VM backend uses git-http (9420); the git:// daemon is left out.
        self.assertIn("BOT_BOTTLE_GATEWAY_DAEMONS=egress,git-http,supervise", init)
        # Role-scoped control-plane auth (issue #469 review): the orchestrator
        # gets the signing key, the gateway daemons get a pre-minted `gateway`
        # JWT — never open mode in the infra VM.
        self.assertIn("host_control_plane_token", init)  # key generated on the volume
        self.assertIn("mint, ROLE_GATEWAY", init)         # gateway JWT minted from it
        self.assertIn('BOT_BOTTLE_CONTROL_PLANE_TOKEN="$CP_KEY" python3 -m bot_bottle.orchestrator',
                      init)  # key -> orchestrator only
        self.assertIn('BOT_BOTTLE_CONTROL_AUTH_JWT="$GW_JWT"', init)  # JWT -> gateway daemons


class TestSshGatewayTransport(unittest.TestCase):
    def test_cp_into_preserves_source_mode(self):
        import os
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


class TestGatewayCaPem(unittest.TestCase):
    def test_dies_when_cert_never_appears(self) -> None:
        from subprocess import CompletedProcess
        infra = infra_vm.InfraVm(vm=None, guest_ip="10.0.0.1", private_key=Path("/k"))
        with patch.object(infra_vm.subprocess, "run",
                          return_value=CompletedProcess([], 1, stdout="", stderr="")), \
             self.assertRaises(SystemExit):
            infra.gateway_ca_pem(timeout=0)


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
    def test_adopts_when_healthy_and_version_matches(self):
        # Healthy control plane + existing key + matching version marker
        # -> adopt (no boot), vm=None.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "id_ed25519").write_text("k")
            (d / "booted-version").write_text("v-current\n")
            with patch.object(infra_vm, "_infra_dir", return_value=d), \
                 patch.object(infra_vm, "_expected_version", return_value="v-current"), \
                 patch.object(infra_vm, "_health_ok", return_value=True), \
                 patch.object(infra_vm, "boot") as boot:
                infra = infra_vm.ensure_running()
        boot.assert_not_called()
        self.assertIsNone(infra.vm)

    def test_reboots_when_version_stale(self):
        # Healthy control plane but the running VM booted an OLDER image
        # (marker mismatch) -> reboot rather than adopt stale code.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "id_ed25519").write_text("k")
            (d / "booted-version").write_text("v-old\n")
            with patch.object(infra_vm, "_infra_dir", return_value=d), \
                 patch.object(infra_vm, "_expected_version", return_value="v-current"), \
                 patch.object(infra_vm, "_health_ok", return_value=True), \
                 patch.object(infra_vm, "stop") as stop, \
                 patch.object(infra_vm, "ensure_built"), \
                 patch.object(infra_vm, "wait_for_health"), \
                 patch.object(infra_vm, "boot") as boot:
                boot.return_value = infra_vm.InfraVm(
                    guest_ip="10.243.255.1", private_key=Path("/k"), vm=MagicMock())
                infra_vm.ensure_running()
            stop.assert_called_once()      # dislodge the outdated VM
            boot.assert_called_once()
            # The fresh boot records the current version for the next launcher.
            self.assertEqual("v-current\n", (d / "booted-version").read_text())

    def test_boots_when_unhealthy(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with patch.object(infra_vm, "_infra_dir", return_value=Path(td)), \
                 patch.object(infra_vm, "_expected_version", return_value="v-current"), \
                 patch.object(infra_vm, "_health_ok", return_value=False), \
                 patch.object(infra_vm, "stop") as stop, \
                 patch.object(infra_vm, "ensure_built") as built, \
                 patch.object(infra_vm, "boot") as boot, \
                 patch.object(infra_vm, "wait_for_health") as wait:
                boot.return_value = infra_vm.InfraVm(
                    guest_ip="10.243.255.1", private_key=Path("/k"), vm=MagicMock())
                infra_vm.ensure_running()
        stop.assert_called_once()      # clear a stale VM first
        built.assert_called_once()
        boot.assert_called_once()
        wait.assert_called_once()


class TestKillPidfile(unittest.TestCase):
    def test_noop_when_no_pidfile(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with patch.object(infra_vm, "_pid_file", return_value=Path(td) / "vm.pid"), \
                 patch.object(infra_vm.os, "kill") as kill:
                infra_vm._kill_pidfile()  # must not raise
            kill.assert_not_called()

    def test_skips_dead_or_recycled_pid(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            pidf = Path(td) / "vm.pid"
            pidf.write_text("999999")  # a PID that isn't a live firecracker
            with patch.object(infra_vm, "_pid_file", return_value=pidf), \
                 patch.object(infra_vm.os, "kill") as kill:
                infra_vm._kill_pidfile()
            kill.assert_not_called()


class TestAdoptable(unittest.TestCase):
    def _dir(self, td: str, *, key: bool = True, version: str | None = None) -> Path:
        d = Path(td)
        if key:
            (d / "id_ed25519").write_text("k")
        if version is not None:
            (d / "booted-version").write_text(version + "\n")
        return d

    def test_true_when_key_version_and_health(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = self._dir(td, version="v1")
            with patch.object(infra_vm, "_infra_dir", return_value=d), \
                 patch.object(infra_vm, "_health_ok", return_value=True):
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
                 patch.object(infra_vm, "_health_ok", return_value=True):
                self.assertFalse(infra_vm._adoptable(d / "id_ed25519", "u", "v1"))


class TestKillInfraFirecrackers(unittest.TestCase):
    def _fake_proc(self, root: Path, pid: int, comm: str, cmdline: list[str]) -> None:
        p = root / str(pid)
        p.mkdir()
        (p / "comm").write_text(comm + "\n")
        (p / "cmdline").write_bytes(b"\0".join(a.encode() for a in cmdline) + b"\0")

    def test_kills_only_matching_infra_firecracker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, \
             tempfile.TemporaryDirectory() as proc:
            infra_dir = Path(td)
            cfg = str(infra_dir / "config.json")
            root = Path(proc)
            # target: firecracker bound to the infra config -> killed
            self._fake_proc(root, 111, "firecracker",
                            ["firecracker", "--no-api", "--config-file", cfg])
            # a firecracker for a different (interactive) VM -> spared
            self._fake_proc(root, 222, "firecracker",
                            ["firecracker", "--config-file", "/home/u/other.json"])
            # a non-firecracker process on the same config path -> spared
            self._fake_proc(root, 333, "python3", ["python3", cfg])
            (root / "not-a-pid").mkdir()
            with patch.object(infra_vm, "_infra_dir", return_value=infra_dir), \
                 patch.object(infra_vm.os, "kill") as kill:
                infra_vm._kill_infra_firecrackers(proc_root=root)
            kill.assert_called_once_with(111, infra_vm.signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
