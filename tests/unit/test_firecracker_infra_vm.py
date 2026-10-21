"""Unit tests for the Firecracker infra-VM substrate (boot primitives + inits).

The KVM boot / HTTP reachability is integration-tested on a KVM host; here we
cover the per-plane role inits, rootfs build, the secret-push retry, and the
pidfile/adoption helpers. The pair coordinator is tested in test_firecracker_infra.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.contract.hostctl import InfraSettings, Settings
from bot_bottle.backend.firecracker import infra_vm


class TestPushSecret(unittest.TestCase):
    """`push_secret` is the shared substrate both services seed a guest secret
    over SSH with (atomic write, retry while SSH comes up). The per-plane pushes
    live with each service (orchestrator: signing key; gateway: JWT)."""

    def _infra(self) -> infra_vm.InfraVm:
        return infra_vm.InfraVm(guest_ip="10.0.0.1", private_key=Path("/k"))

    def test_written_atomically(self):
        proc = MagicMock(returncode=0, stderr="")
        with patch.object(infra_vm.subprocess, "run", return_value=proc) as run:
            infra_vm.push_secret(self._infra(), "secret", "/dest", "the thing")
        self.assertEqual("secret", run.call_args.kwargs["input"])
        remote_cmd = run.call_args.args[0][-1]
        self.assertIn("cat > /dest.tmp", remote_cmd)
        self.assertIn("mv /dest.tmp /dest", remote_cmd)

    def test_dies_when_push_never_succeeds(self):
        proc = MagicMock(returncode=255, stderr="ssh: connect refused")
        with patch.object(infra_vm, "_SECRET_PUSH_TIMEOUT_SECONDS", 0.05), \
                patch.object(infra_vm, "_SECRET_PUSH_POLL_SECONDS", 0.0), \
                patch.object(infra_vm.subprocess, "run", return_value=proc), \
                patch.object(infra_vm, "die", side_effect=SystemExit) as die:
            with self.assertRaises(SystemExit):
                infra_vm.push_secret(self._infra(), "secret", "/dest", "the thing")
        die.assert_called_once()


class TestRoleInits(unittest.TestCase):
    def test_orchestrator_init_starts_only_the_control_plane(self):
        init = infra_vm.role_init("orchestrator")
        # No bb_role branch — the rootfs *is* the role.
        self.assertNotIn("bb_role=", init)
        self.assertNotIn("bot_bottle.gateway.bootstrap", init)
        self.assertIn("export PATH=", init)  # shared preamble
        # Persistent registry volume mounted at the DB dir on the orchestrator.
        self.assertIn("/dev/vdb", init)
        # Persistent buildah store mounted where image_builder points --root.
        self.assertIn(f"mount -t ext4 /dev/vdc {infra_vm.GUEST_BUILD_STORE}", init)
        self.assertIn(f"cat {infra_vm._GUEST_SIGNING_KEY_PATH}", init)  # host-seeded key
        self.assertIn("refusing to start the control plane", init)     # no open mode
        self.assertIn('BOT_BOTTLE_ORCHESTRATOR_TOKEN="$CP_KEY" \\\n', init)  # key -> orchestrator only
        self.assertIn("python3 -m bot_bottle.orchestrator", init)
        self.assertIn("--db /var/lib/bot-bottle/db/bot-bottle.db", init)

    def test_gateway_init_starts_only_the_data_plane(self):
        init = infra_vm.role_init("gateway")
        self.assertNotIn("bb_role=", init)
        self.assertNotIn("bot_bottle.orchestrator", init)
        self.assertNotIn("/dev/vdb", init)  # no registry volume on the data plane
        self.assertNotIn("/dev/vdc", init)  # no build store either
        self.assertIn("export PATH=", init)  # shared preamble
        self.assertIn("BOT_BOTTLE_GATEWAY_DAEMONS=egress,git-http,supervise", init)
        self.assertIn(f"cat {infra_vm._GUEST_GATEWAY_JWT_PATH}", init)  # host-minted JWT
        self.assertNotIn("ROLE_GATEWAY", init)  # minting moved to the host
        self.assertIn("refusing to start the data plane", init)        # fail-closed
        self.assertIn('BOT_BOTTLE_ORCHESTRATOR_AUTH_JWT="$GW_JWT"', init)  # JWT -> daemons
        # The gateway resolves the orchestrator's address off the cmdline
        # (bb_orch), so no IP is baked into the artifact.
        self.assertIn("bb_orch=", init)
        self.assertIn("BOT_BOTTLE_ORCHESTRATOR_URL=http://$ORCH:", init)


class TestBuildRootfsDir(unittest.TestCase):
    def test_orchestrator_rootfs_uses_the_fc_image_and_role_variant(self):
        with patch.object(infra_vm.util, "build_base_rootfs_dir") as build:
            build.return_value = Path("/cache/rootfs/x")
            infra_vm.build_rootfs_dir("orchestrator", InfraSettings())
        self.assertEqual(infra_vm._ORCHESTRATOR_FC_IMAGE, build.call_args.args[0])
        self.assertTrue(build.call_args.kwargs["variant"].startswith("-orchestrator-"))

    def test_gateway_rootfs_uses_the_gateway_image_directly(self):
        with patch.object(infra_vm.util, "build_base_rootfs_dir") as build:
            build.return_value = Path("/cache/rootfs/x")
            infra_vm.build_rootfs_dir("gateway", InfraSettings())
        self.assertEqual(infra_vm._GATEWAY_IMAGE, build.call_args.args[0])
        self.assertTrue(build.call_args.kwargs["variant"].startswith("-gateway-"))


class TestEnsureBuilt(unittest.TestCase):
    def test_default_pulls_both_artifacts_without_docker(self):
        # PRD 0069 Stage 2: the launch host pulls the prebuilt rootfs; no Docker.
        settings = Settings()
        with patch.object(infra_vm.infra_rootfs, "builds_locally", return_value=False), \
                patch.object(infra_vm.docker_mod, "build_image") as build, \
                patch.object(infra_vm.infra_rootfs, "ensure_available") as pull:
            infra_vm.ensure_built(settings)
        build.assert_not_called()
        # Fetched up front, both planes together: booting one and then failing
        # to fetch the other leaves half a control plane running.
        pull.assert_called_once_with(settings.infra)

    def test_unmatched_published_images_without_docker_die_clearly(self):
        # Installed from main past the last release, on a host with no Docker:
        # neither pulling (stale) nor building works, so say so.
        with patch.object(infra_vm.infra_rootfs, "builds_locally", return_value=True), \
                patch.object(infra_vm.shutil, "which", return_value=None), \
                patch.object(infra_vm, "die", side_effect=SystemExit) as die, \
                self.assertRaises(SystemExit):
            infra_vm.ensure_built(Settings())
        self.assertIn("no published infra image matches", die.call_args.args[0])

    def test_local_mode_builds_orchestrator_before_its_fc_image(self):
        local = Settings(infra=InfraSettings(infra_build_local=True))
        with patch.object(infra_vm.docker_mod, "build_image") as build, \
                patch.object(
                    infra_vm.docker_mod,
                    "pinned_local_image_ref",
                    return_value="bot-bottle-orchestrator:sha256-" + "a" * 64,
                ) as pinned_ref:
            infra_vm.ensure_built(local)
        tags = [c.args[0] for c in build.call_args_list]
        # orchestrator-fc is FROM orchestrator, so the base is built first.
        self.assertIn(infra_vm._ORCHESTRATOR_IMAGE, tags)
        self.assertIn(infra_vm._GATEWAY_IMAGE, tags)
        self.assertEqual(infra_vm._ORCHESTRATOR_FC_IMAGE, tags[-1])
        self.assertLess(tags.index(infra_vm._ORCHESTRATOR_IMAGE),
                        tags.index(infra_vm._ORCHESTRATOR_FC_IMAGE))
        pinned_ref.assert_called_once_with(infra_vm._ORCHESTRATOR_IMAGE)
        self.assertEqual(
            {
                "ORCHESTRATOR_BASE_IMAGE":
                    "bot-bottle-orchestrator:sha256-" + "a" * 64,
            },
            build.call_args_list[-1].kwargs["build_args"],
        )


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
            # Both VMs' pidfiles are reaped — plus the legacy pre-split combined
            # VM's — and the version marker is gone so a stopped pair is never
            # adopted.
            self.assertEqual(3, kill.call_count)
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
                self.assertTrue(infra_vm.adoptable(d / "id_ed25519", "u", "v1"))

    def test_false_when_key_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = self._dir(td, key=False, version="v1")
            with patch.object(infra_vm, "_infra_dir", return_value=d):
                self.assertFalse(infra_vm.adoptable(d / "id_ed25519", "u", "v1"))

    def test_false_when_no_version_marker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = self._dir(td)  # key present, no booted-version
            with patch.object(infra_vm, "_infra_dir", return_value=d):
                self.assertFalse(infra_vm.adoptable(d / "id_ed25519", "u", "v1"))

    def test_false_when_version_mismatch(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = self._dir(td, version="v-old")
            with patch.object(infra_vm, "_infra_dir", return_value=d), \
                 patch.object(infra_vm, "_health_ok", return_value=True), \
                 patch.object(infra_vm, "_pidfile_alive", return_value=True):
                self.assertFalse(infra_vm.adoptable(d / "id_ed25519", "u", "v1"))

    def test_false_when_gateway_dead(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = self._dir(td, version="v1")
            with patch.object(infra_vm, "_infra_dir", return_value=d), \
                 patch.object(infra_vm, "_health_ok", return_value=True), \
                 patch.object(infra_vm, "_pidfile_alive", return_value=False):
                self.assertFalse(infra_vm.adoptable(d / "id_ed25519", "u", "v1"))


class TestKillInfraFirecrackers(unittest.TestCase):
    def _fake_proc(self, root: Path, pid: int, comm: str, cmdline: list[str]) -> None:
        p = root / str(pid)
        p.mkdir()
        (p / "comm").write_text(comm + "\n")
        (p / "cmdline").write_bytes(b"\0".join(a.encode() for a in cmdline) + b"\0")

    def test_kills_both_infra_and_legacy_firecrackers_only(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td, \
             tempfile.TemporaryDirectory() as proc:
            infra_dir = Path(td)
            orch_cfg = str(infra_dir / "orchestrator" / "config.json")
            gw_cfg = str(infra_dir / "gateway" / "config.json")
            legacy_cfg = str(infra_dir / "config.json")  # pre-split combined VM
            root = Path(proc)
            # both new infra VMs + the legacy combined VM -> killed
            self._fake_proc(root, 111, "firecracker",
                            ["firecracker", "--no-api", "--config-file", orch_cfg])
            self._fake_proc(root, 112, "firecracker",
                            ["firecracker", "--no-api", "--config-file", gw_cfg])
            self._fake_proc(root, 113, "firecracker",
                            ["firecracker", "--no-api", "--config-file", legacy_cfg])
            # a firecracker for a different (interactive) VM -> spared
            self._fake_proc(root, 222, "firecracker",
                            ["firecracker", "--config-file", "/home/u/other.json"])
            # a non-firecracker process on an infra config path -> spared
            self._fake_proc(root, 333, "python3", ["python3", orch_cfg])
            (root / "not-a-pid").mkdir()
            with patch.object(infra_vm, "_infra_dir", return_value=infra_dir), \
                 patch.object(infra_vm, "_orch_dir", return_value=infra_dir / "orchestrator"), \
                 patch.object(infra_vm, "_gw_dir", return_value=infra_dir / "gateway"), \
                 patch.object(infra_vm.os, "kill") as kill:
                infra_vm._kill_infra_firecrackers(proc_root=root)
            killed = {c.args[0] for c in kill.call_args_list}
            self.assertEqual({111, 112, 113}, killed)


if __name__ == "__main__":
    unittest.main()
