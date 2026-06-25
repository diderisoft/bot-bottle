"""Gitea deploy-key provisioner (PRD 0048, contrib).

Generates ed25519 keypairs via `ssh-keygen` and registers / deletes
them using the Gitea deploy-key HTTP API. No new Python dependencies —
only stdlib `urllib.request` and `subprocess`.

Required token permissions (Gitea "Applications" → "Generate Token"):
  - Repository: Read & Write
    Grants POST /api/v1/repos/{owner}/{repo}/keys (create deploy key)
    and DELETE /api/v1/repos/{owner}/{repo}/keys/{id} (revoke deploy key).
    No other scopes are needed."""

from __future__ import annotations

import json
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from ...deploy_key_provisioner import DeployKeyCollisionError, DeployKeyProvisioner


class GiteaDeployKeyProvisioner(DeployKeyProvisioner):
    """Manages deploy keys on a Gitea instance."""

    def __init__(self, *, token: str, api_url: str) -> None:
        self._token = token
        self._api_url = api_url.rstrip("/")

    def create(self, owner_repo: str, title: str) -> tuple[str, bytes]:
        """Generate an ed25519 keypair, register the public half as a
        repo deploy key, and return `(key_id, private_key_bytes)`.

        The key is registered with `read_only=False` because git-gate
        needs push access to forward gitleaks-scanned refs upstream."""
        with tempfile.TemporaryDirectory() as tmpdir:
            key_path = Path(tmpdir) / "key"
            subprocess.run(
                [
                    "ssh-keygen", "-t", "ed25519",
                    "-f", str(key_path),
                    "-N", "",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            private_key = key_path.read_bytes()
            public_key = key_path.with_suffix(".pub").read_text().strip()

        owner, repo = _split_owner_repo(owner_repo)
        url = f"{self._api_url}/api/v1/repos/{owner}/{repo}/keys"
        payload = json.dumps({
            "key": public_key,
            "read_only": False,
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
            with urllib.request.urlopen(req) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            _body = _read_error_body(exc)
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

    def delete(self, owner_repo: str, key_id: str) -> None:
        """Delete the deploy key. HTTP 404 (already gone) is success.
        All other errors raise RuntimeError so teardown halts."""
        owner, repo = _split_owner_repo(owner_repo)
        url = f"{self._api_url}/api/v1/repos/{owner}/{repo}/keys/{key_id}"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"token {self._token}"},
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(req):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return
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


def _split_owner_repo(owner_repo: str) -> tuple[str, str]:
    """Split `'owner/repo'` into `('owner', 'repo')`."""
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
