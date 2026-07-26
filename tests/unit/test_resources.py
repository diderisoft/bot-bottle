"""Unit: bot_bottle.resources — build-resource resolution for both a source
checkout and an installed wheel.

The checkout path is what the whole test suite already runs under; the wheel
path is exercised here by faking an installed layout (a package dir with a
bundled ``_resources/`` and no sibling Dockerfiles) and asserting that
``build_root()`` stages a repo-root-shaped context. See
``test_wheel_install.py`` for the end-to-end build+install check.
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from bot_bottle import resources

from tests.unit import use_bottle_root


class TestCheckoutMode(unittest.TestCase):
    """The environment the suite runs in: a real source checkout."""

    def test_is_source_checkout(self):
        self.assertTrue(resources.is_source_checkout())

    def test_build_root_is_repo_root(self):
        root = resources.build_root()
        self.assertTrue((root / "bot_bottle").is_dir())
        self.assertTrue((root / "pyproject.toml").is_file())
        self.assertTrue((root / "Dockerfile.gateway").is_file())

    def test_resource_helpers_resolve(self):
        self.assertTrue(resources.dockerfile("Dockerfile.gateway").is_file())
        self.assertTrue(resources.nix_netpool_module().is_file())
        self.assertTrue(resources.netpool_script().is_file())

    def test_image_build_args_come_from_the_central_input_file(self):
        root = resources.build_root()
        args = resources.image_build_args(
            "Dockerfile.gateway",
            context=root,
        )
        self.assertEqual({"PYTHON_BASE_IMAGE"}, set(args))
        self.assertRegex(
            args["PYTHON_BASE_IMAGE"],
            r"^python:\d+\.\d+\.\d+-.+@sha256:[0-9a-f]{64}$",
        )

    def test_generated_dockerfile_uses_central_input_from_build_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            dockerfile = Path(tmp) / "Dockerfile"
            dockerfile.write_text(
                "ARG DOCKER_CLI_BASE_IMAGE\n"
                "FROM ${DOCKER_CLI_BASE_IMAGE}\n",
                encoding="utf-8",
            )
            args = resources.image_build_args(dockerfile, context=tmp)
        self.assertEqual({"DOCKER_CLI_BASE_IMAGE"}, set(args))
        self.assertRegex(
            args["DOCKER_CLI_BASE_IMAGE"],
            r"^docker:\d+-cli@sha256:[0-9a-f]{64}$",
        )

    def test_bundled_resources_all_exist_at_root(self):
        # Drift guard: every path setup.py bundles must exist in the checkout.
        root = resources.build_root()
        for rel in resources.BUNDLED_RESOURCES:
            self.assertTrue((root / rel).is_file(), f"missing bundled resource: {rel}")


class TestWheelMode(unittest.TestCase):
    """Fake an installed wheel: a package dir with _resources/ and no
    checkout Dockerfiles beside it."""

    def _fake_install(self, tmp: Path) -> Path:
        pkg = tmp / "site-packages" / "bot_bottle"
        (pkg / "cli").mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "cli" / "__init__.py").write_text("# module\n")
        bundled = pkg / "_resources"
        for rel in resources.BUNDLED_RESOURCES:
            dst = bundled / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(f"# fake {rel}\n")
        return pkg

    @contextmanager
    def _wheel(self):
        """Point `resources` at a fake installed wheel with an isolated
        app-data dir; yields the package dir so a test can mutate it."""
        with tempfile.TemporaryDirectory() as tmpname:
            tmp = Path(tmpname)
            pkg = self._fake_install(tmp)
            self.addCleanup(use_bottle_root(tmp / "appdata"))
            with patch.object(resources, "_PKG", pkg), \
                    patch.object(resources, "_BUNDLED", pkg / "_resources"), \
                    patch.object(resources, "_CHECKOUT_ROOT", tmp / "no-checkout"):
                yield pkg

    def test_stage_and_resolve(self):
        with self._wheel():
            self.assertFalse(resources.is_source_checkout())
            root = resources.build_root()
            # Staged context looks like a repo root.
            self.assertTrue((root / "bot_bottle" / "__init__.py").is_file())
            self.assertTrue((root / "bot_bottle" / "cli" / "__init__.py").is_file())
            self.assertTrue((root / "pyproject.toml").is_file())
            self.assertTrue((root / "Dockerfile.gateway").is_file())
            self.assertTrue((root / "nix" / "firecracker-netpool.nix").is_file())
            self.assertTrue((root / "scripts" / "firecracker-netpool.sh").is_file())
            # The bundled-resource copies are NOT re-nested under the staged
            # package (keeps it byte-identical to a checkout package).
            self.assertFalse((root / "bot_bottle" / "_resources").exists())
            # Helpers resolve off the staged root.
            self.assertEqual(root / "Dockerfile.gateway",
                             resources.dockerfile("Dockerfile.gateway"))
            # Idempotent: second call returns the same completed dir.
            self.assertEqual(root, resources.build_root())

    def test_refreshes_when_content_changes_at_same_version(self):
        # Regression for the stale-cache bug: `pipx install --force` of a newer
        # commit keeps version 0.1.0, so keying on version would reuse the old
        # tree. Keying on content must re-stage when a package file changes.
        with self._wheel() as pkg:
            root1 = resources.build_root()
            self.assertTrue((root1 / ".complete").is_file())
            (pkg / "cli" / "__init__.py").write_text("# new commit, same version\n")
            root2 = resources.build_root()
            self.assertNotEqual(root1, root2)
            self.assertEqual(
                "# new commit, same version\n",
                (root2 / "bot_bottle" / "cli" / "__init__.py").read_text(),
            )

    def test_failed_stage_cleans_up_temp_dir(self):
        # A failure mid-stage must not leave a half-written temp dir behind.
        with self._wheel():
            base = resources.bot_bottle_root() / "build-root"
            with patch.object(resources.shutil, "copytree", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    resources.build_root()
            self.assertEqual([], list(base.glob(".staging-*")))

    def test_rebuilds_when_stage_incomplete(self):
        # A crash mid-stage can leave a dir without its `.complete` marker; the
        # next call must rebuild it rather than trust the partial tree.
        with self._wheel():
            root = resources.build_root()
            (root / ".complete").unlink()
            (root / "sentinel").write_text("stale")
            again = resources.build_root()
            self.assertEqual(root, again)  # same content digest → same dir
            self.assertTrue((again / ".complete").is_file())
            self.assertFalse((again / "sentinel").exists())  # rebuilt clean

    def test_reuses_peer_stage_after_lock_wait(self):
        # Regression for the staging race: a caller that loses the lock must,
        # once it wins, see the peer's completed tree and reuse it — never
        # re-clobber a shared path. Drive it deterministically: hold the lock,
        # let a worker block after its fast-path miss, publish a complete tree
        # as the "peer", then release so the worker takes the reuse path.
        import fcntl
        import threading
        import time

        with self._wheel():
            base = resources.bot_bottle_root() / "build-root"
            base.mkdir(parents=True, exist_ok=True)
            dest = base / resources._content_digest()  # pylint: disable=protected-access

            result: dict[str, Path] = {}
            with open(base / ".stage.lock", "w", encoding="utf-8") as held:
                fcntl.flock(held, fcntl.LOCK_EX)

                def worker() -> None:
                    result["root"] = resources.build_root()

                t = threading.Thread(target=worker)
                t.start()
                # Let the worker miss the fast path (dest not yet complete) and
                # block on the held lock, then publish a complete tree as a peer
                # would have and release the lock.
                time.sleep(0.3)
                dest.mkdir(parents=True)
                (dest / ".complete").write_text("")
                fcntl.flock(held, fcntl.LOCK_UN)
                t.join(timeout=10)

            self.assertEqual(dest, result["root"])
            self.assertFalse(t.is_alive())

    def test_missing_bundle_raises(self):
        with tempfile.TemporaryDirectory() as tmpname:
            tmp = Path(tmpname)
            pkg = tmp / "bot_bottle"
            pkg.mkdir()
            restore = use_bottle_root(tmp / "appdata")
            self.addCleanup(restore)
            with patch.object(resources, "_PKG", pkg), \
                    patch.object(resources, "_BUNDLED", pkg / "_resources"), \
                    patch.object(resources, "_CHECKOUT_ROOT", tmp / "no-checkout"):
                with self.assertRaises(resources.ResourceError):
                    resources.build_root()


if __name__ == "__main__":
    unittest.main()
