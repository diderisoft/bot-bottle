"""Unit: smolmachines `_ensure_smolmachine` agent-image pipeline
(PRD 0023 chunk 4c).

Asserts that the cache-hit path returns without touching the
registry / pack pipeline, and that the cache-miss path runs
build → tag → push → pack in order against a registry port the
helper yields.

The pipeline lives in `launch.py` (moved from `prepare.py` so the
docker build doesn't run before the dashboard's preflight modal;
the curses-endwin / tmux pane-routing handoff happens around
`launch`)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.backend.smolmachines import launch as _launch_mod


class TestEnsureSmolmachine(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cb-cache.")
        self._cache_patch = patch.object(
            _launch_mod, "_SMOLMACHINE_CACHE_DIR", Path(self._tmp.name),
        )
        self._cache_patch.start()

    def tearDown(self):
        self._cache_patch.stop()
        self._tmp.cleanup()

    def test_cache_hit_skips_registry_and_pack(self):
        # Pre-populate the cache for image id `sha256:abcdef0123456789...`.
        digest = "abcdef0123456789"
        sidecar = Path(self._tmp.name) / f"{digest}.smolmachine.smolmachine"
        sidecar.write_text("")

        with patch.object(
            _launch_mod.docker_mod, "build_image",
        ) as build, patch.object(
            _launch_mod.docker_mod, "image_id",
            return_value=f"sha256:{digest}fffffffffffffffff",
        ), patch.object(
            _launch_mod.docker_mod, "save",
        ) as save, patch.object(
            _launch_mod, "ephemeral_registry",
        ) as registry, patch.object(
            _launch_mod, "crane_push_tarball",
        ) as push, patch.object(
            _launch_mod._smolvm, "pack_create",
        ) as pack:
            result = _launch_mod._ensure_smolmachine("bot-bottle-claude:latest")

        self.assertEqual(sidecar, result)
        # build still runs (Dockerfile edits land without manual rmi).
        build.assert_called_once()
        # No save (500MB tarball), no registry, no push, no pack on
        # cache hit.
        save.assert_not_called()
        registry.assert_not_called()
        push.assert_not_called()
        pack.assert_not_called()

    def test_cache_miss_runs_build_save_push_pack_in_order(self):
        digest = "0123456789abcdef"

        # ephemeral_registry yields a RegistryHandle with the
        # docker network + a push endpoint (container DNS) and
        # pull endpoint (host port-forward).
        from bot_bottle.backend.smolmachines.local_registry import (
            RegistryHandle,
        )

        class _Reg:
            def __enter__(self_inner):  # type: ignore
                return RegistryHandle(
                    network="cb-net-xyz",
                    push_endpoint="cb-registry-xyz:5000",
                    pull_endpoint="localhost:54321",
                )
            def __exit__(self_inner, *exc):  # type: ignore
                return False

        calls: list[str] = []

        def record(name):  # type: ignore
            def _f(*a, **kw):  # type: ignore
                calls.append(name)
            return _f

        with patch.object(
            _launch_mod.docker_mod, "build_image",
            side_effect=record("build"),
        ), patch.object(
            _launch_mod.docker_mod, "image_id",
            return_value=f"sha256:{digest}fffffffffffffffff",
        ), patch.object(
            _launch_mod.docker_mod, "save",
            side_effect=record("save"),
        ) as save, patch.object(
            _launch_mod, "ephemeral_registry",
            return_value=_Reg(),
        ), patch.object(
            _launch_mod, "crane_push_tarball",
            side_effect=record("push"),
        ) as push, patch.object(
            _launch_mod._smolvm, "pack_create",
            side_effect=record("pack"),
        ) as pack:
            _launch_mod._ensure_smolmachine("bot-bottle-claude:latest")

        # Build → save → push → pack in that order. No `docker
        # push` (the daemon's HTTPS-by-default path is what we're
        # sidestepping).
        self.assertEqual(["build", "save", "push", "pack"], calls)

        # docker save targets a per-digest tarball alongside the
        # cached sidecar.
        save_args = save.call_args.args
        self.assertEqual("bot-bottle-claude:latest", save_args[0])
        self.assertTrue(save_args[1].endswith(f"{digest}.image.tar"))

        # crane push runs against the push_endpoint (container DNS
        # on the registry network) with the digest as the tag.
        push_args = push.call_args.args
        self.assertEqual(
            f"cb-registry-xyz:5000/bot-bottle:{digest}", push_args[2],
        )

        # pack_create reads from the pull_endpoint (host port-
        # forward, smolvm is on the host). Same repo+tag, just a
        # different routing hostname — the registry stores one blob.
        pack_args = pack.call_args.args
        self.assertEqual(
            f"localhost:54321/bot-bottle:{digest}", pack_args[0],
        )
        self.assertTrue(str(pack_args[1]).endswith(f"{digest}.smolmachine"))


if __name__ == "__main__":
    unittest.main()
