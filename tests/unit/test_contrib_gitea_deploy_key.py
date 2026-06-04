"""Unit: GiteaDeployKeyProvisioner (PRD 0048, contrib/gitea)."""

from __future__ import annotations

import json
import unittest
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

from bot_bottle.contrib.gitea.deploy_key_provisioner import (
    GiteaDeployKeyProvisioner,
    _split_owner_repo,
)


def _provisioner() -> GiteaDeployKeyProvisioner:
    return GiteaDeployKeyProvisioner(
        token="test-token", api_url="https://gitea.example.com"
    )


def _urlopen_response(body: dict, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode()
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _http_error(code: int, body: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://x",
        code=code,
        msg="err",
        hdrs=None,  # type: ignore[arg-type]
        fp=BytesIO(body.encode()),
    )


class TestCreate(unittest.TestCase):
    def test_create_calls_ssh_keygen_and_posts_to_api(self):
        provisioner = _provisioner()
        fake_key_id = 42
        fake_private = b"PRIVATE_KEY"
        fake_public = "ssh-ed25519 AAAA fake"

        with patch(
            "bot_bottle.contrib.gitea.deploy_key_provisioner.subprocess.run"
        ) as mock_run, patch(
            "bot_bottle.contrib.gitea.deploy_key_provisioner.urllib.request.urlopen"
        ) as mock_urlopen, patch(
            "bot_bottle.contrib.gitea.deploy_key_provisioner.Path.read_bytes",
            return_value=fake_private,
        ), patch(
            "bot_bottle.contrib.gitea.deploy_key_provisioner.Path.read_text",
            return_value=fake_public + "\n",
        ):
            mock_urlopen.return_value = _urlopen_response({"id": fake_key_id})
            key_id, private_bytes = provisioner.create(
                "didericis/bot-bottle", "bot-bottle:slug:repo"
            )

        # ssh-keygen called with ed25519
        mock_run.assert_called_once()
        run_args = mock_run.call_args.args[0]
        self.assertIn("ssh-keygen", run_args)
        self.assertIn("-t", run_args)
        self.assertIn("ed25519", run_args)

        # POST body contains public key
        post_call = mock_urlopen.call_args.args[0]
        payload = json.loads(post_call.data)
        self.assertEqual(fake_public, payload["key"])
        self.assertFalse(payload["read_only"])

        # Correct URL
        self.assertIn(
            "/api/v1/repos/didericis/bot-bottle/keys", post_call.full_url
        )
        self.assertEqual(str(fake_key_id), key_id)
        self.assertEqual(fake_private, private_bytes)

    def test_create_raises_on_http_error(self):
        provisioner = _provisioner()
        with patch(
            "bot_bottle.contrib.gitea.deploy_key_provisioner.subprocess.run"
        ), patch(
            "bot_bottle.contrib.gitea.deploy_key_provisioner.urllib.request.urlopen",
            side_effect=_http_error(403, "forbidden"),
        ), patch(
            "bot_bottle.contrib.gitea.deploy_key_provisioner.Path.read_bytes",
            return_value=b"pk",
        ), patch(
            "bot_bottle.contrib.gitea.deploy_key_provisioner.Path.read_text",
            return_value="ssh-ed25519 AAAA\n",
        ):
            with self.assertRaises(RuntimeError) as ctx:
                provisioner.create("owner/repo", "title")
        self.assertIn("403", str(ctx.exception))


class TestDelete(unittest.TestCase):
    def test_delete_calls_correct_endpoint(self):
        provisioner = _provisioner()
        with patch(
            "bot_bottle.contrib.gitea.deploy_key_provisioner.urllib.request.urlopen"
        ) as mock_urlopen:
            mock_urlopen.return_value = _urlopen_response({})
            provisioner.delete("didericis/bot-bottle", "99")

        req = mock_urlopen.call_args.args[0]
        self.assertIn("/api/v1/repos/didericis/bot-bottle/keys/99", req.full_url)
        self.assertEqual("DELETE", req.get_method())

    def test_delete_tolerates_404(self):
        provisioner = _provisioner()
        with patch(
            "bot_bottle.contrib.gitea.deploy_key_provisioner.urllib.request.urlopen",
            side_effect=_http_error(404),
        ):
            provisioner.delete("owner/repo", "123")  # must not raise

    def test_delete_raises_on_non_404_http_error(self):
        provisioner = _provisioner()
        with patch(
            "bot_bottle.contrib.gitea.deploy_key_provisioner.urllib.request.urlopen",
            side_effect=_http_error(500, "internal server error"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                provisioner.delete("owner/repo", "7")
        self.assertIn("500", str(ctx.exception))

    def test_delete_raises_on_url_error(self):
        provisioner = _provisioner()
        with patch(
            "bot_bottle.contrib.gitea.deploy_key_provisioner.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                provisioner.delete("owner/repo", "7")
        self.assertIn("connection refused", str(ctx.exception))


class TestSplitOwnerRepo(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(("owner", "repo"), _split_owner_repo("owner/repo"))

    def test_raises_on_missing_slash(self):
        with self.assertRaises(ValueError):
            _split_owner_repo("noslash")

    def test_raises_on_empty_owner(self):
        with self.assertRaises(ValueError):
            _split_owner_repo("/repo")

    def test_raises_on_empty_repo(self):
        with self.assertRaises(ValueError):
            _split_owner_repo("owner/")


if __name__ == "__main__":
    unittest.main()
