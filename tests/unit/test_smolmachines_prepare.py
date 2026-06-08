"""Unit: smolmachines prepare.py env resolution (PRD 0038)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bot_bottle.agent_provider import AgentProvisionPlan
from bot_bottle.env import ResolvedEnv


class TestSmolmachinesResolveEnv(unittest.TestCase):
    """resolve_plan() must call resolve_env() and build guest_env
    from the resolved values rather than from raw bottle.env."""

    def _run_resolve_plan(
        self,
        resolved: ResolvedEnv,
        *,
        extra_host_env: dict[str, str] | None = None,
    ) -> dict[str, str]:
        from bot_bottle.backend import BottleSpec
        from bot_bottle.manifest import Manifest

        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "stage"
            stage.mkdir()

            # Minimal manifest with one env literal so the spec is valid.
            manifest = Manifest.from_json_obj({
                "agents": {"myagent": {"bottle": "mybottle"}},
                "bottles": {"mybottle": {"env": {"PLAIN": "literal-value"}}},
            })
            spec = BottleSpec(
                manifest=manifest,
                agent_name="myagent",
                copy_cwd=False,
                user_cwd=tmp,
                identity="test-slug-00001",
            )

            from bot_bottle import supervise as _sup
            orig_root = _sup.bot_bottle_root
            _sup.bot_bottle_root = lambda: Path(tmp) / ".bot-bottle"  # type: ignore[assignment]

            host_env = {**os.environ, **(extra_host_env or {})}  # type: ignore

            try:
                with (
                    patch("bot_bottle.backend.smolmachines.resolve_plan.resolve_env",
                          return_value=resolved) as mock_resolve,
                    patch("bot_bottle.backend.smolmachines.resolve_plan.smolmachines_preflight"),
                    patch("bot_bottle.backend.smolmachines.resolve_plan.smolmachines_bundle_subnet",
                          return_value=("10.99.0.0/24", "10.99.0.1", "10.99.0.2")),
                    patch("bot_bottle.backend.resolve_common.GitGate") as mock_gg,
                    patch("bot_bottle.backend.resolve_common.Egress") as mock_eg,
                    patch("bot_bottle.backend.resolve_common.Supervise"),
                    patch(
                        "bot_bottle.backend.smolmachines.resolve_plan.agent_provision_plan"
                    ) as mock_app,
                ):
                    mock_gg.return_value.prepare.return_value = MagicMock()
                    mock_eg.return_value.prepare.return_value = MagicMock()
                    def _make_provision(**kwargs):  # type: ignore
                        return AgentProvisionPlan(
                            template="claude",
                            command="claude",
                            prompt_mode="append_file",
                            dockerfile="",
                            image="bot-bottle-claude:latest",
                            guest_env=dict(kwargs.get("guest_env") or {}),
                        )
                    mock_app.side_effect = lambda **kw: _make_provision(**kw)  # type: ignore

                    from bot_bottle.backend.smolmachines.resolve_plan import resolve_plan
                    plan = resolve_plan(spec, stage_dir=stage)

                mock_resolve.assert_called_once_with(manifest, "myagent")
                return dict(plan.guest_env)
            finally:
                _sup.bot_bottle_root = orig_root  # type: ignore[assignment]

    def test_literal_env_reaches_guest_env(self):
        resolved = ResolvedEnv(
            literals={"PLAIN": "hello"},
            forwarded={},
        )
        guest_env = self._run_resolve_plan(resolved)
        self.assertEqual("hello", guest_env["PLAIN"])

    def test_forwarded_env_reaches_guest_env(self):
        # Secrets / interpolated values land in forwarded; they must
        # still reach the guest (argv exposure is the known gap).
        resolved = ResolvedEnv(
            literals={},
            forwarded={"SECRET": "s3cr3t", "INTERP": "resolved-val"},
        )
        guest_env = self._run_resolve_plan(resolved)
        self.assertEqual("s3cr3t", guest_env["SECRET"])
        self.assertEqual("resolved-val", guest_env["INTERP"])

    def test_raw_manifest_sentinel_not_in_guest_env(self):
        # Before the fix, ?prompt and ${HOST} would appear verbatim.
        # After the fix, resolve_env() is called so the caller sees
        # the mocked resolved values (no raw sentinel survives).
        resolved = ResolvedEnv(
            literals={},
            forwarded={"MY_SECRET": "actual-value"},
        )
        guest_env = self._run_resolve_plan(resolved)
        for v in guest_env.values():
            self.assertFalse(
                v.startswith("?"),
                f"raw secret sentinel survived in guest_env: {v!r}",
            )
            self.assertFalse(
                v.startswith("${"),
                f"raw interpolation sentinel survived in guest_env: {v!r}",
            )

    def test_tls_trust_env_always_present(self):
        resolved = ResolvedEnv(literals={}, forwarded={})
        guest_env = self._run_resolve_plan(resolved)
        for key in ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
            self.assertIn(key, guest_env, f"{key} missing from guest_env")


if __name__ == "__main__":
    unittest.main()
