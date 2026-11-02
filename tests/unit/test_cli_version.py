"""Unit: bot-bottle version command."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from bot_bottle.cli.commands import version as version_cmd
from bot_bottle.control.client import OrchestratorEndpoint


def _run(argv: list[str] | None = None) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = version_cmd.cmd_version(argv or [])
    return code, out.getvalue()


def _endpoint(url: str = "http://orch:8099") -> OrchestratorEndpoint:
    return OrchestratorEndpoint("docker", url)


class TestVersionNoOrchestrator(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch.object(version_cmd, "install_version",
                                            return_value=("1.2.3", "abc123")))
        self.enterContext(mock.patch.object(version_cmd, "recorded_orchestrator",
                                            return_value=None))
        self.enterContext(mock.patch.object(version_cmd, "load_artifacts_manifest_optional",
                                            return_value=None))

    def test_returns_zero(self) -> None:
        code, _ = _run()
        self.assertEqual(0, code)

    def test_shows_cli_version_and_protocol(self) -> None:
        _, out = _run()
        self.assertIn("1.2.3", out)
        self.assertIn("(abc123)", out)
        self.assertIn("protocol", out)

    def test_shows_orchestrator_not_running(self) -> None:
        _, out = _run()
        self.assertIn("not running", out)

    def test_no_commit_omits_suffix(self) -> None:
        with mock.patch.object(version_cmd, "install_version", return_value=("1.0.0", "")):
            _, out = _run()
        self.assertNotIn("()", out)

    def test_no_manifest_shows_hint(self) -> None:
        _, out = _run()
        self.assertIn("no published-artifacts.json", out)

    def test_empty_manifest_shows_nothing(self) -> None:
        with mock.patch.object(version_cmd, "load_artifacts_manifest_optional", return_value={}):
            _, out = _run()
        self.assertNotIn("agent images", out)


class TestVersionWithOrchestrator(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch.object(version_cmd, "install_version",
                                            return_value=("1.2.3", "")))
        self.enterContext(mock.patch.object(version_cmd, "recorded_orchestrator",
                                            return_value=_endpoint()))
        self.enterContext(mock.patch.object(version_cmd, "load_artifacts_manifest_optional",
                                            return_value=None))

    def test_shows_orchestrator_url(self) -> None:
        client = mock.MagicMock()
        client.version.return_value = {"version": "1.2.3", "commit": "abc", "protocol": 1,
                                       "images": {}}
        with mock.patch.object(version_cmd, "OrchestratorClient", return_value=client):
            _, out = _run()
        self.assertIn("http://orch:8099", out)
        self.assertIn("(abc)", out)

    def test_not_answering_shows_hint(self) -> None:
        client = mock.MagicMock()
        client.version.return_value = None
        with mock.patch.object(version_cmd, "OrchestratorClient", return_value=client):
            _, out = _run()
        self.assertIn("not answering", out)
        self.assertIn("http://orch:8099", out)

    def test_published_image(self) -> None:
        digest = "sha256:" + "a" * 64
        client = mock.MagicMock()
        client.version.return_value = {"version": "1.0", "commit": "", "protocol": 1,
                                       "images": {"bot-bottle-orchestrator": {
                                           "source": "published", "digest": digest,
                                           "inputs_hash": "dead1234"}}}
        with mock.patch.object(version_cmd, "OrchestratorClient", return_value=client):
            _, out = _run()
        self.assertIn("published", out)
        self.assertIn("dead1234", out)

    def test_local_image(self) -> None:
        client = mock.MagicMock()
        client.version.return_value = {"version": "1.0", "commit": "", "protocol": 1,
                                       "images": {"bot-bottle-gateway": {
                                           "source": "local", "inputs_hash": "beef5678"}}}
        with mock.patch.object(version_cmd, "OrchestratorClient", return_value=client):
            _, out = _run()
        self.assertIn("local", out)
        self.assertIn("beef5678", out)

    def test_unknown_image_source(self) -> None:
        client = mock.MagicMock()
        client.version.return_value = {"version": "1.0", "commit": "", "protocol": 1,
                                       "images": {"bot-bottle-gateway": {"source": "custom"}}}
        with mock.patch.object(version_cmd, "OrchestratorClient", return_value=client):
            _, out = _run()
        self.assertIn("custom", out)

    def test_non_dict_image_entry_skipped(self) -> None:
        client = mock.MagicMock()
        client.version.return_value = {"version": "1.0", "commit": "", "protocol": 1,
                                       "images": {"bot-bottle-gateway": "bad"}}
        with mock.patch.object(version_cmd, "OrchestratorClient", return_value=client):
            code, _ = _run()
        self.assertEqual(0, code)


class TestVersionWithManifest(unittest.TestCase):
    def _make_entry(self, name: str, has_artifact: bool = True) -> mock.MagicMock:
        from bot_bottle.published_artifacts import FORMAT_OCI
        entry = mock.MagicMock()
        entry.version = "0.9.0"
        artifact = mock.MagicMock()
        artifact.digest = "sha256:" + "b" * 64
        artifact.format = FORMAT_OCI
        artifact.platform = "linux/amd64"
        entry.for_format.return_value = artifact if has_artifact else None
        entry.artifacts = [artifact]
        return entry

    def setUp(self) -> None:
        self.enterContext(mock.patch.object(version_cmd, "install_version",
                                            return_value=("1.0.0", "")))
        self.enterContext(mock.patch.object(version_cmd, "recorded_orchestrator",
                                            return_value=None))
        self.enterContext(mock.patch.object(version_cmd, "current_platform",
                                            return_value="linux/amd64"))
        from bot_bottle.image_inputs import INFRA_ROLES
        agent_name = "bot-bottle-claude"
        manifest = {agent_name: self._make_entry(agent_name)}
        # Ensure infra names don't appear as agent names
        for role in INFRA_ROLES:
            manifest.pop(role, None)
        self.manifest = manifest

    def test_shows_agent_images(self) -> None:
        with mock.patch.object(version_cmd, "load_artifacts_manifest_optional",
                               return_value=self.manifest):
            _, out = _run()
        self.assertIn("agent images", out)
        self.assertIn("claude", out)
        self.assertIn("0.9.0", out)

    def test_missing_platform_artifact(self) -> None:
        entry = self._make_entry("bot-bottle-claude", has_artifact=False)
        with mock.patch.object(version_cmd, "load_artifacts_manifest_optional",
                               return_value={"bot-bottle-claude": entry}):
            _, out = _run()
        self.assertIn("no linux/amd64 artifact", out)


if __name__ == "__main__":
    unittest.main()
