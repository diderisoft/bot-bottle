"""Gitea deploy-key provisioner: `ssh-keygen` keypairs registered and deleted
through Gitea's deploy-key API (stdlib only).

The token needs Repository: Read & Write (POST and DELETE
/api/v1/repos/{owner}/{repo}/keys)."""

from __future__ import annotations

import json
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from ...deploy_key_provisioner import (
    DeployKeyCollisionError, DeployKeyProvisioner, ForgeTokenRefusedError,
)

# Bounds ssh-keygen and Gitea calls so a hung Gitea can't stall a launch.
_API_TIMEOUT_SECS = 30
_KEYGEN_TIMEOUT_SECS = 10


class GiteaDeployKeyProvisioner(DeployKeyProvisioner):
    """Deploy keys on a Gitea instance."""

    def __init__(self, *, token: str, api_url: str) -> None:
        self._token = token
        self._api_url = api_url.rstrip("/")

    def create(self, owner_repo: str, title: str, *,
               read_only: bool = False) -> tuple[str, bytes]:
        """Generate an ed25519 keypair, register the public half as a deploy
        key (writable unless `read_only`; git-gate pushes with it), and return
        `(key_id, private_key_bytes)`."""
        private_key, public_key = generate_keypair()
        owner, repo = _split_owner_repo(owner_repo)
        url = f"{self._api_url}/api/v1/repos/{owner}/{repo}/keys"
        payload = json.dumps({
            "key": public_key,
            "read_only": read_only,
            "title": title,
        }).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"token {self._token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_API_TIMEOUT_SECS) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            _body = _read_error_body(exc)
            if exc.code in (401, 403):
                raise ForgeTokenRefusedError(self._refused(exc.code)) from exc
            if exc.code == 422:
                raise DeployKeyCollisionError(
                    f"deploy key collision for {owner_repo!r} "
                    f"(title={title!r}): key title or content already registered — {_body}"
                ) from exc
            raise RuntimeError(
                f"failed to create deploy key for {owner_repo}: "
                f"HTTP {exc.code} — {_body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"failed to create deploy key for {owner_repo}: {exc.reason}"
            ) from exc

        return str(body["id"]), private_key

    def _refused(self, code: int) -> str:
        return (f"{self._api_url} refused this host's Gitea token (HTTP {code}): it was "
                f"revoked, expired or lost its scopes. Connect the forge again with "
                f"`bot-bottle forge connect {self._api_url}`, or from the host's Forges "
                f"page in the console")

    def delete(self, owner_repo: str, key_id: str) -> None:
        """Delete the deploy key; 404 is success. Other errors raise
        RuntimeError."""
        owner, repo = _split_owner_repo(owner_repo)
        url = f"{self._api_url}/api/v1/repos/{owner}/{repo}/keys/{key_id}"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"token {self._token}"},
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(req, timeout=_API_TIMEOUT_SECS):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return
            if exc.code in (401, 403):
                raise ForgeTokenRefusedError(self._refused(exc.code)) from exc
            _body = _read_error_body(exc)
            raise RuntimeError(
                f"failed to delete deploy key {key_id} for {owner_repo}: "
                f"HTTP {exc.code} — {_body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"failed to delete deploy key {key_id} for {owner_repo}: "
                f"{exc.reason}"
            ) from exc


def generate_keypair() -> tuple[bytes, str]:
    """A fresh ed25519 keypair from `ssh-keygen`: (private key, public key line).
    Every forge kind's provisioner mints its keys with it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        key_path = Path(tmpdir) / "key"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", ""],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_KEYGEN_TIMEOUT_SECS,
        )
        return key_path.read_bytes(), key_path.with_suffix(".pub").read_text().strip()


def _split_owner_repo(owner_repo: str) -> tuple[str, str]:
    """`'owner/repo'` -> `('owner', 'repo')`."""
    parts = owner_repo.split("/", 1)
    if len(parts) != 2 or not all(parts):
        raise ValueError(
            f"expected 'owner/repo' format, got {owner_repo!r}"
        )
    return parts[0], parts[1]


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: broad-exception-caught — safely fallback to empty error message
        return ""


# Shared with the other forge kinds' provisioners.
split_owner_repo = _split_owner_repo
read_error_body = _read_error_body
