"""GitHub deploy-key provisioner: `ssh-keygen` keypairs registered and deleted
through GitHub's deploy-key API (stdlib only).

The token needs Administration: read & write on the repo (POST and DELETE
/repos/{owner}/{repo}/keys); a classic token, the `repo` scope."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from ...deploy_key_provisioner import (
    DeployKeyCollisionError, DeployKeyProvisioner, DeployKeysDisabledError,
)
from ..gitea.deploy_key_provisioner import generate_keypair, read_error_body, split_owner_repo
from .forge_api import api_base, headers

# Bounds the GitHub calls so a hung API can't stall a launch.
_API_TIMEOUT_SECS = 30


class GitHubDeployKeyProvisioner(DeployKeyProvisioner):
    """Deploy keys on github.com or a GitHub Enterprise Server."""

    def __init__(self, *, token: str, api_url: str) -> None:
        self._token = token
        self._api = api_base(api_url)

    def create(self, owner_repo: str, title: str, *,
               read_only: bool = False) -> tuple[str, bytes]:
        """Generate an ed25519 keypair, register the public half as a deploy
        key (writable unless `read_only`), and return `(key_id, private_key)`."""
        private_key, public_key = generate_keypair()
        owner, repo = split_owner_repo(owner_repo)
        req = urllib.request.Request(
            f"{self._api}/repos/{owner}/{repo}/keys",
            data=json.dumps({"title": title, "key": public_key,
                             "read_only": read_only}).encode(),
            headers={**headers(self._token), "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_API_TIMEOUT_SECS) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = read_error_body(exc)
            if exc.code == 422 and "deploy keys are disabled" in detail.lower():
                raise DeployKeysDisabledError(
                    f"GitHub has deploy keys turned off for {owner_repo}: allow them in the "
                    f"repo's or its organization's settings (an org policy overrides the "
                    f"repo's), or reach it over HTTPS with your GitHub token instead (drop "
                    f"`transport: deploy-key` from the forge), then grant it again") from exc
            if exc.code == 422:
                raise DeployKeyCollisionError(
                    f"deploy key collision for {owner_repo!r} (title={title!r}): "
                    f"key already in use — {detail}") from exc
            hint = (" (the token needs Administration: read & write on the repo)"
                    if exc.code in (403, 404) else "")
            raise RuntimeError(f"failed to create deploy key for {owner_repo}: "
                               f"HTTP {exc.code}{hint} — {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"failed to create deploy key for {owner_repo}: {exc.reason}") from exc
        return str(body["id"]), private_key

    def delete(self, owner_repo: str, key_id: str) -> None:
        """Delete the deploy key; 404 is success. Other errors raise
        RuntimeError."""
        owner, repo = split_owner_repo(owner_repo)
        req = urllib.request.Request(f"{self._api}/repos/{owner}/{repo}/keys/{key_id}",
                                     headers=headers(self._token), method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=_API_TIMEOUT_SECS):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return
            raise RuntimeError(f"failed to delete deploy key {key_id} for {owner_repo}: "
                               f"HTTP {exc.code} — {read_error_body(exc)}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"failed to delete deploy key {key_id} for {owner_repo}: "
                               f"{exc.reason}") from exc
