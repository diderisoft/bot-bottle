"""Unit: smolmachines `_ensure_smolmachine` agent-image pipeline
(PRD 0023 chunk 4c).

Asserts that the cache-hit path returns without touching the
registry / pack pipeline, and that the cache-miss path runs
build → tag → push → pack in order against a registry port the
helper yields."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_bottle.backend.smolmachines import prepare as _prepare


class TestEnsureSmolmachine(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cb-cache.")
        self._cache_patch = patch.object(
            _prepare, "_SMOLMACHINE_CACHE_DIR", Path(self._tmp.name),
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
            _prepare.docker_mod, "build_image",
        ) as build, patch.object(
            _prepare.docker_mod, "image_id",
            return_value=f"sha256:{digest}fffffffffffffffff",
        ), patch.object(
            _prepare, "ephemeral_registry",
        ) as registry, patch.object(
            _prepare.docker_mod, "tag",
        ) as tag, patch.object(
            _prepare.docker_mod, "push",
        ) as push, patch.object(
            _prepare._smolvm, "pack_create",
        ) as pack:
            result = _prepare._ensure_smolmachine("claude-bottle:latest")

        self.assertEqual(sidecar, result)
        # build still runs (Dockerfile edits land without manual rmi)
        build.assert_called_once()
        # No registry, no tag, no push, no pack on cache hit.
        registry.assert_not_called()
        tag.assert_not_called()
        push.assert_not_called()
        pack.assert_not_called()

    def test_cache_miss_runs_build_tag_push_pack_in_order(self):
        digest = "0123456789abcdef"

        # ephemeral_registry yields a RegistryEndpoints with two
        # routing hostnames — daemon-side for docker push,
        # host-side for smolvm pack create.
        from claude_bottle.backend.smolmachines.local_registry import (
            RegistryEndpoints,
        )

        class _Reg:
            def __enter__(self_inner):
                return RegistryEndpoints(
                    daemon_endpoint="host.docker.internal:54321",
                    host_endpoint="localhost:54321",
                )
            def __exit__(self_inner, *exc):
                return False

        calls: list[str] = []

        def record(name):
            def _f(*a, **kw):
                calls.append(name)
            return _f

        with patch.object(
            _prepare.docker_mod, "build_image",
            side_effect=record("build"),
        ), patch.object(
            _prepare.docker_mod, "image_id",
            return_value=f"sha256:{digest}fffffffffffffffff",
        ), patch.object(
            _prepare, "ephemeral_registry",
            return_value=_Reg(),
        ), patch.object(
            _prepare.docker_mod, "tag",
            side_effect=record("tag"),
        ) as tag, patch.object(
            _prepare.docker_mod, "push",
            side_effect=record("push"),
        ) as push, patch.object(
            _prepare._smolvm, "pack_create",
            side_effect=record("pack"),
        ) as pack:
            _prepare._ensure_smolmachine("claude-bottle:latest")

        # build first (no point pushing if the build fails), then
        # tag → push → pack against the registry endpoints.
        self.assertEqual(["build", "tag", "push", "pack"], calls)

        # tag + push target the daemon-side endpoint (host.docker
        # .internal on Docker Desktop, since the daemon's
        # localhost is its own VM's loopback).
        tag_args = tag.call_args.args
        self.assertEqual("claude-bottle:latest", tag_args[0])
        self.assertEqual(
            f"host.docker.internal:54321/claude-bottle:{digest}", tag_args[1],
        )
        push_args = push.call_args.args
        self.assertEqual(
            f"host.docker.internal:54321/claude-bottle:{digest}", push_args[0],
        )
        # pack_create reads from the host-side endpoint (smolvm is
        # a host process and can only resolve localhost). The
        # registry stores images by repo+tag, so both endpoints
        # hit the same blob.
        pack_args = pack.call_args.args
        self.assertEqual(
            f"localhost:54321/claude-bottle:{digest}", pack_args[0],
        )
        self.assertTrue(str(pack_args[1]).endswith(f"{digest}.smolmachine"))


if __name__ == "__main__":
    unittest.main()
