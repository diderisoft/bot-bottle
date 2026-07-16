"""Unit tests for the Firecracker infra VM (control-plane VM boot).

The KVM boot / HTTP reachability is integration-tested on a KVM host; here
we cover the URL shape, the rootfs-variant wiring, and the health-poll
decisions that must hold without a VM.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.backend.firecracker import infra_vm


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
        self.assertEqual("-infra", build.call_args.kwargs["variant"])
        # The init runs BOTH the control plane and the gateway data plane,
        # and exports PATH so gateway_init's subprocess daemons find python3.
        init = build.call_args.kwargs["init_script"]
        self.assertIn("bot_bottle.orchestrator", init)
        self.assertIn("gateway_init.py", init)
        self.assertIn("export PATH=", init)


class TestEnsureBuilt(unittest.TestCase):
    def test_builds_gateway_then_infra(self):
        with patch.object(infra_vm.docker_mod, "build_image") as build:
            infra_vm.ensure_built()
        tags = [c.args[0] for c in build.call_args_list]
        self.assertEqual([infra_vm._GATEWAY_IMAGE, infra_vm._INFRA_IMAGE], tags)


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
        infra.vm.process.returncode = 1
        with patch.object(infra_vm.firecracker_vm, "_console_tail", return_value=""), \
             self.assertRaises(SystemExit):
            infra_vm.wait_for_health(infra, timeout=5)


if __name__ == "__main__":
    unittest.main()
