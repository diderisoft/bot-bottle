"""Tests for the backend-aware capability probes and skip guards
(``tests/_backend.py``) and the CI preflight (``tests/backend_preflight.py``).

The probes shell out to Docker / inspect ``/dev/kvm``; here they are driven
entirely through mocks so the unit job asserts the decision logic without
needing either backend present on the runner.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tests import _backend, backend_preflight
from tests._backend import (
    Capability,
    backend_capability,
    docker_capability,
    firecracker_capability,
    selected_backend,
    skip_unless_backend,
    skip_unless_selected_backend_available,
)


def _skipped(decorated: type) -> bool:
    return getattr(decorated, "__unittest_skip__", False)


def _new_case() -> type:
    return type("Case", (unittest.TestCase,), {})


class TestSelectedBackend(unittest.TestCase):
    def test_defaults_to_docker_when_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual("docker", selected_backend())

    def test_reads_env(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_BACKEND": "firecracker"}, clear=True):
            self.assertEqual("firecracker", selected_backend())


class TestDockerCapability(unittest.TestCase):
    def test_opt_out_via_skip_docker_tests(self):
        with patch.dict(os.environ, {"SKIP_DOCKER_TESTS": "1"}, clear=True):
            cap = docker_capability()
        self.assertFalse(cap.ok)
        self.assertIn("SKIP_DOCKER_TESTS", cap.detail)

    def test_not_on_path(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("tests._backend.shutil.which", return_value=None):
            cap = docker_capability()
        self.assertFalse(cap.ok)
        self.assertIn("PATH", cap.detail)

    def test_daemon_reachable(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("tests._backend.shutil.which", return_value="/usr/bin/docker"), \
                patch("tests._backend.subprocess.run") as run:
            run.return_value.returncode = 0
            cap = docker_capability()
        self.assertTrue(cap.ok)

    def test_daemon_unreachable(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("tests._backend.shutil.which", return_value="/usr/bin/docker"), \
                patch("tests._backend.subprocess.run") as run:
            run.return_value.returncode = 1
            cap = docker_capability()
        self.assertFalse(cap.ok)
        self.assertIn("unreachable", cap.detail)


class TestFirecrackerCapability(unittest.TestCase):
    def test_kvm_missing(self):
        with patch("tests._backend.os.path.exists", return_value=False):
            cap = firecracker_capability()
        self.assertFalse(cap.ok)
        self.assertIn("KVM", cap.detail)

    def test_kvm_inaccessible(self):
        with patch("tests._backend.os.path.exists", return_value=True), \
                patch("tests._backend.os.access", return_value=False):
            cap = firecracker_capability()
        self.assertFalse(cap.ok)
        self.assertIn("kvm group", cap.detail)

    def test_binary_missing(self):
        with patch("tests._backend.os.path.exists", return_value=True), \
                patch("tests._backend.os.access", return_value=True), \
                patch("tests._backend.shutil.which", return_value=None):
            cap = firecracker_capability()
        self.assertFalse(cap.ok)
        self.assertIn("firecracker not on PATH", cap.detail)

    def test_ready(self):
        with patch("tests._backend.os.path.exists", return_value=True), \
                patch("tests._backend.os.access", return_value=True), \
                patch("tests._backend.shutil.which", return_value="/usr/bin/firecracker"):
            cap = firecracker_capability()
        self.assertTrue(cap.ok)


class TestBackendCapability(unittest.TestCase):
    def test_unknown_backend(self):
        cap = backend_capability("qemu")
        self.assertFalse(cap.ok)
        self.assertIn("no capability probe", cap.detail)

    def test_defaults_to_selected(self):
        def probe() -> Capability:
            return Capability("docker", True, "ok")

        with patch.dict(os.environ, {"BOT_BOTTLE_BACKEND": "docker"}, clear=True), \
                patch.dict("tests._backend._PROBES", {"docker": probe}):
            cap = backend_capability()
        self.assertEqual("docker", cap.backend)
        self.assertTrue(cap.ok)


class TestSkipUnlessBackend(unittest.TestCase):
    def test_skips_when_other_backend_selected(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_BACKEND": "firecracker"}, clear=True):
            decorated = skip_unless_backend("docker")(_new_case())
        self.assertTrue(_skipped(decorated))

    def test_runs_when_selected_and_capable(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_BACKEND": "docker"}, clear=True), \
                patch("tests._backend.backend_capability",
                      return_value=Capability("docker", True, "ok")):
            decorated = skip_unless_backend("docker")(_new_case())
        self.assertFalse(_skipped(decorated))

    def test_skips_when_selected_but_incapable(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_BACKEND": "docker"}, clear=True), \
                patch("tests._backend.backend_capability",
                      return_value=Capability("docker", False, "docker daemon unreachable")):
            decorated = skip_unless_backend("docker")(_new_case())
        self.assertTrue(_skipped(decorated))


class TestSkipUnlessSelectedBackendAvailable(unittest.TestCase):
    def test_runs_when_selected_backend_capable(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_BACKEND": "firecracker"}, clear=True), \
                patch("tests._backend.backend_capability",
                      return_value=Capability("firecracker", True, "ok")):
            decorated = skip_unless_selected_backend_available()(_new_case())
        self.assertFalse(_skipped(decorated))

    def test_skips_when_selected_backend_incapable(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_BACKEND": "firecracker"}, clear=True), \
                patch("tests._backend.backend_capability",
                      return_value=Capability("firecracker", False, "/dev/kvm missing")):
            decorated = skip_unless_selected_backend_available()(_new_case())
        self.assertTrue(_skipped(decorated))


class TestPreflightMain(unittest.TestCase):
    def test_pass_exits_zero(self):
        with patch.object(backend_preflight, "backend_capability",
                          return_value=Capability("docker", True, "docker daemon reachable")):
            rc = backend_preflight.main(["docker"])
        self.assertEqual(0, rc)

    def test_fail_exits_nonzero(self):
        with patch.object(backend_preflight, "backend_capability",
                          return_value=Capability("firecracker", False, "/dev/kvm missing")):
            rc = backend_preflight.main(["firecracker"])
        self.assertEqual(1, rc)

    def test_defaults_to_selected_backend(self):
        with patch.dict(os.environ, {"BOT_BOTTLE_BACKEND": "docker"}, clear=True), \
                patch.object(backend_preflight, "backend_capability",
                             return_value=Capability("docker", True, "ok")) as cap:
            rc = backend_preflight.main([])
        self.assertEqual(0, rc)
        cap.assert_called_once_with("docker")

    def test_module_exposes_selected_backend(self):
        # Import-surface guard: the preflight re-exports the selector it uses.
        self.assertIs(backend_preflight.selected_backend, _backend.selected_backend)


if __name__ == "__main__":
    unittest.main()
