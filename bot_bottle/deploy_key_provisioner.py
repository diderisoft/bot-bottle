"""Deploy-key provisioner interface and factory. Implementations live in
`bot_bottle/contrib/<provider>/` and are imported lazily."""

from __future__ import annotations

import subprocess
import tempfile
import urllib.error
from abc import ABC, abstractmethod
from pathlib import Path

_KEYGEN_TIMEOUT_SECS = 10


class DeployKeyCollisionError(RuntimeError):
    """A deploy key with this title or public key already exists on the repo."""


class DeployKeysDisabledError(RuntimeError):
    """The forge won't take deploy keys for the repo (on GitHub, an org or repo
    policy). The message says what to change; it's shown to the user."""


class ForgeTokenRefusedError(RuntimeError):
    """The forge refused the token itself (401/403): it was revoked, expired
    or lost its scopes, so retrying won't help. The message says how to fix it."""


class DeployKeyProvisioner(ABC):
    """One deploy key's lifecycle on a forge."""

    @abstractmethod
    def create(self, owner_repo: str, title: str, *,
               read_only: bool = False) -> tuple[str, bytes]:
        """Generate a keypair and register the public half as a deploy key on
        `owner_repo` (`<owner>/<repo>`) under `title`, unable to push when
        `read_only`. Returns `(key_id, private_key_bytes)`; `key_id` is only
        passed back to `delete`."""

    @abstractmethod
    def delete(self, owner_repo: str, key_id: str) -> None:
        """Delete the key. An already-absent key is success; any other failure
        raises so teardown halts."""


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


def split_owner_repo(owner_repo: str) -> tuple[str, str]:
    """`'owner/repo'` -> `('owner', 'repo')`."""
    parts = owner_repo.split("/", 1)
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"expected 'owner/repo' format, got {owner_repo!r}")
    return parts[0], parts[1]


def read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: broad-exception-caught
        return ""


def get_provisioner(
    provider: str, token: str, api_url: str
) -> DeployKeyProvisioner:
    """The provisioner for `provider`. Raises `ManifestError` for an unknown
    one."""
    if provider == "gitea":
        from bot_bottle.contrib.gitea.deploy_key_provisioner import (
            GiteaDeployKeyProvisioner,
        )
        return GiteaDeployKeyProvisioner(token=token, api_url=api_url)
    if provider == "github":
        from bot_bottle.contrib.github.deploy_key_provisioner import (
            GitHubDeployKeyProvisioner,
        )
        return GitHubDeployKeyProvisioner(token=token, api_url=api_url)
    from .manifest.util import ManifestError
    raise ManifestError(
        f"unknown provisioned_key provider: {provider!r}; "
        f"available: gitea, github"
    )
