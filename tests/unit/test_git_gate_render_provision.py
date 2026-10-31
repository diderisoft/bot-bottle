"""Unit: git_gate gitconfig rendering + deploy-key provision/revoke
(coverage ratchet, ADR 0004).

Covers the pure `git_gate_render_gitconfig` renderer and the dynamic
(gitea) deploy-key lifecycle, with the forge provisioner mocked."""

from __future__ import annotations

import os
import socket
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from bot_bottle.errors import MissingEnvVarError
from bot_bottle.secret_source import MappingSecrets
from bot_bottle.control.git_gate_render import (
    _gitconfig_validate_value,
    git_gate_render_gitconfig,
)
from bot_bottle.control.git_gate_render import GitGateUpstream
from bot_bottle.git_gate import (
    _provision_dynamic_key,
    revoke_git_gate_provisioned_keys,
)
from bot_bottle.git_gate.plan import GitGatePlan
from bot_bottle.git_gate.provision import provision_git_gate_dynamic_keys
from bot_bottle.manifest.git import ManifestGitEntry, ManifestKeyConfig


def _entry(**kw: Any) -> ManifestGitEntry:
    base: dict[str, Any] = {
        "Name": "repo",
        "Upstream": "git@github.com:o/r.git",
        "UpstreamHost": "github.com",
        "UpstreamUser": "git",
        "UpstreamPath": "o/r.git",
        "UpstreamPort": "22",
    }
    base.update(kw)
    return ManifestGitEntry(**base)


def _gitea_entry(**kw: Any) -> ManifestGitEntry:
    return _entry(
        Key=ManifestKeyConfig(provider="gitea", forge_token_env="GITEA_TOK"),
        **kw,
    )


class _FakeProvisioner:
    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, str]] = []

    def create(self, owner_repo: str, title: str, *,
               read_only: bool = False) -> tuple[str, bytes]:
        self.created.append((owner_repo, title))
        self.read_only = read_only
        return "kid123", b"PRIVATE-KEY-BYTES"

    def delete(self, owner_repo: str, key_id: str) -> None:
        self.deleted.append((owner_repo, key_id))


# ---------------------------------------------------------------------------
# git_gate_render_gitconfig
# ---------------------------------------------------------------------------


class TestRenderGitconfig(unittest.TestCase):
    def test_empty_entries_returns_empty_string(self) -> None:
        self.assertEqual("", git_gate_render_gitconfig((), "git-gate"))

    def test_single_entry_renders_insteadof(self) -> None:
        out = git_gate_render_gitconfig((_entry(),), "git-gate")
        self.assertIn('[url "http://git-gate/repo.git"]', out)
        self.assertIn("insteadOf = git@github.com:o/r.git", out)

    def test_identity_token_extraheader_over_http(self) -> None:
        # A URL-scoped http.extraHeader, so git-http can check (source_ip, token).
        out = git_gate_render_gitconfig(
            (_entry(),), "1.2.3.4:9420", identity_token="TOK123")
        self.assertIn('[http "http://1.2.3.4:9420/"]', out)
        self.assertIn("extraHeader = x-bot-bottle-identity: TOK123", out)

    def test_remote_key_alias_with_nondefault_port(self) -> None:
        out = git_gate_render_gitconfig(
            (_entry(RemoteKey="10.0.0.5", UpstreamPort="2222"),), "git-gate",
        )
        self.assertIn("insteadOf = ssh://git@10.0.0.5:2222/o/r.git", out)

    def test_remote_key_alias_default_port_omits_port(self) -> None:
        out = git_gate_render_gitconfig(
            (_entry(RemoteKey="10.0.0.5", UpstreamPort="22"),), "git-gate",
        )
        self.assertIn("insteadOf = ssh://git@10.0.0.5/o/r.git", out)
        self.assertNotIn(":22/", out)

    def test_validate_rejects_newline(self) -> None:
        with self.assertRaises(ValueError):
            _gitconfig_validate_value("field", "line1\nline2")

    def test_render_rejects_newline_in_upstream(self) -> None:
        with self.assertRaises(ValueError):
            git_gate_render_gitconfig((_entry(Upstream="a\nb"),), "git-gate")


_TOKEN = MappingSecrets({"GITEA_TOK": "secret-token"})


# ---------------------------------------------------------------------------
# _provision_dynamic_key
# ---------------------------------------------------------------------------


class TestProvisionDynamicKey(unittest.TestCase):
    def test_happy_path_writes_key_and_id(self) -> None:
        fake = _FakeProvisioner()
        with tempfile.TemporaryDirectory() as d, \
                patch("bot_bottle.deploy_key_provisioner.get_provisioner", return_value=fake), \
                patch.dict(os.environ, {"BOT_BOTTLE_ENV": ""}), \
                patch("sys.stderr"):
            path = _provision_dynamic_key(_gitea_entry(), "myslug", Path(d), _TOKEN)
            key_file = Path(path)
            self.assertEqual(b"PRIVATE-KEY-BYTES", key_file.read_bytes())
            id_file = Path(d) / "repo-deploy-key-id"
            self.assertEqual("kid123", id_file.read_text())
        # owner_repo had .git stripped; title carries globalize_slug(slug) + name
        hostname = socket.gethostname()
        self.assertEqual([("o/r", f"bot-bottle:{hostname}-myslug:repo")], fake.created)

    def test_a_named_environment_is_in_the_title(self) -> None:
        # Its environments share the hostname, and a forge wants titles
        # unique per repo.
        fake = _FakeProvisioner()
        with tempfile.TemporaryDirectory() as d, \
                patch("bot_bottle.deploy_key_provisioner.get_provisioner", return_value=fake), \
                patch.dict(os.environ, {"BOT_BOTTLE_ENV": "dev"}), \
                patch("sys.stderr"):
            _provision_dynamic_key(_gitea_entry(), "myslug", Path(d), _TOKEN)
        hostname = socket.gethostname()
        self.assertEqual([("o/r", f"bot-bottle:{hostname}-dev-myslug:repo")], fake.created)

    def test_missing_token_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(MissingEnvVarError):
                _provision_dynamic_key(_gitea_entry(), "s", Path(d), MappingSecrets({}))


def _gate_plan(d: str) -> GitGatePlan:
    return GitGatePlan(slug="myslug", hook_script=Path(d) / "hook",
                       access_hook_script=Path(d) / "access",
                       upstreams=(GitGateUpstream(name="repo", upstream_url="ssh://x/o/r.git",
                                                  upstream_host="x", upstream_port="22",
                                                  identity_file="", known_host_key=""),))


class TestLeftoverKeys(unittest.TestCase):
    """A run killed before teardown leaves its keys registered; the next
    launch under the slug revokes them before minting its own."""

    def test_a_previous_runs_key_is_revoked_first(self) -> None:
        fake = _FakeProvisioner()
        with tempfile.TemporaryDirectory() as d, \
                patch("bot_bottle.deploy_key_provisioner.get_provisioner", return_value=fake), \
                patch("sys.stderr"):
            (Path(d) / "repo-deploy-key-id").write_text("old-kid")
            provision_git_gate_dynamic_keys(_bottle(_gitea_entry()), _gate_plan(d), Path(d),
                                            _TOKEN)
            self.assertEqual("kid123", (Path(d) / "repo-deploy-key-id").read_text())
        self.assertEqual([("o/r", "old-kid")], fake.deleted)
        self.assertEqual(1, len(fake.created))

    def test_a_first_launch_revokes_nothing(self) -> None:
        fake = _FakeProvisioner()
        with tempfile.TemporaryDirectory() as d, \
                patch("bot_bottle.deploy_key_provisioner.get_provisioner", return_value=fake), \
                patch("sys.stderr"):
            provision_git_gate_dynamic_keys(_bottle(_gitea_entry()), _gate_plan(d), Path(d),
                                            _TOKEN)
        self.assertEqual([], fake.deleted)
        self.assertEqual(1, len(fake.created))

    def test_a_key_that_wont_revoke_is_left_and_minting_goes_on(self) -> None:
        fake = _FakeProvisioner()

        def fail(_owner_repo: str, _key_id: str) -> None:
            raise RuntimeError("HTTP 500")
        fake.delete = fail  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as d, \
                patch("bot_bottle.deploy_key_provisioner.get_provisioner", return_value=fake), \
                patch("sys.stderr"):
            (Path(d) / "repo-deploy-key-id").write_text("old-kid")
            provision_git_gate_dynamic_keys(_bottle(_gitea_entry()), _gate_plan(d), Path(d),
                                            _TOKEN)
        self.assertEqual(1, len(fake.created))


# ---------------------------------------------------------------------------
# revoke_git_gate_provisioned_keys
# ---------------------------------------------------------------------------


def _bottle(*entries: ManifestGitEntry) -> Any:
    return cast(Any, types.SimpleNamespace(git=entries))


class TestRevokeProvisionedKeys(unittest.TestCase):
    def test_revokes_gitea_key_when_id_present(self) -> None:
        fake = _FakeProvisioner()
        with tempfile.TemporaryDirectory() as d, \
                patch("bot_bottle.deploy_key_provisioner.get_provisioner", return_value=fake), \
                patch("sys.stderr"):
            (Path(d) / "repo-deploy-key-id").write_text("kid123")
            revoke_git_gate_provisioned_keys(_bottle(_gitea_entry()), Path(d), _TOKEN)
        self.assertEqual([("o/r", "kid123")], fake.deleted)

    def test_skips_non_gitea_entry(self) -> None:
        fake = _FakeProvisioner()
        static_entry = _entry(Key=ManifestKeyConfig(provider="static", path="/k"))
        with tempfile.TemporaryDirectory() as d, \
                patch("bot_bottle.deploy_key_provisioner.get_provisioner", return_value=fake):
            revoke_git_gate_provisioned_keys(_bottle(static_entry), Path(d), _TOKEN)
        self.assertEqual([], fake.deleted)

    def test_skips_when_id_file_missing(self) -> None:
        fake = _FakeProvisioner()
        with tempfile.TemporaryDirectory() as d, \
                patch("bot_bottle.deploy_key_provisioner.get_provisioner", return_value=fake):
            # no id file written -> entry skipped
            revoke_git_gate_provisioned_keys(_bottle(_gitea_entry()), Path(d), _TOKEN)
        self.assertEqual([], fake.deleted)

    def test_missing_token_raises(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "repo-deploy-key-id").write_text("kid123")
            with self.assertRaises(MissingEnvVarError):
                revoke_git_gate_provisioned_keys(
                    _bottle(_gitea_entry()), Path(d), MappingSecrets({}))


if __name__ == "__main__":
    unittest.main()
