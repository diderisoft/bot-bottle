"""GitHub's "Deploy keys are disabled for this repository" (an org or repo
policy) is its own error with what to change, not a "collision", and a grant
shows it to the user instead of "see the host controller's log"."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from typing import Any
from unittest import mock

from bot_bottle.contrib.github.deploy_key_provisioner import GitHubDeployKeyProvisioner
from bot_bottle.deploy_key_provisioner import DeployKeyCollisionError, DeployKeysDisabledError


def _http_422(message: str) -> urllib.error.HTTPError:
    body = json.dumps({"message": "Validation Failed",
                       "errors": [{"resource": "PublicKey", "code": "custom",
                                   "message": message}]}).encode()
    return urllib.error.HTTPError("https://api.github.com/repos/o/r/keys", 422,
                                  "Unprocessable", {}, io.BytesIO(body))  # type: ignore[arg-type]


class TestDeployKeysDisabled(unittest.TestCase):
    def _create(self, error: urllib.error.HTTPError) -> Any:
        prov = GitHubDeployKeyProvisioner(token="t", api_url="https://api.github.com")
        with mock.patch("urllib.request.urlopen", side_effect=error):
            return prov.create("o/r", "bot-bottle:x", read_only=True)

    def test_disabled_is_its_own_error(self) -> None:
        with self.assertRaises(DeployKeysDisabledError) as ctx:
            self._create(_http_422("Deploy keys are disabled for this repository"))
        self.assertIn("organization", str(ctx.exception))
        self.assertIn("o/r", str(ctx.exception))

    def test_a_real_collision_is_still_one(self) -> None:
        with self.assertRaises(DeployKeyCollisionError):
            self._create(_http_422("key is already in use"))

    def test_a_grant_shows_the_reason(self) -> None:
        from bot_bottle.control.hostctl import repo_grants
        with mock.patch.object(repo_grants, "find_forge",
                               side_effect=DeployKeysDisabledError(
                                   "GitHub has deploy keys turned off")):
            reply = repo_grants.grant_repo(mock.Mock(), "b1", "github.com/o/r",
                                           secrets_file=mock.Mock())
        self.assertEqual({"error": "GitHub has deploy keys turned off"}, reply)


if __name__ == "__main__":
    unittest.main()
