"""Deploy-key provisioner interface and factory. Implementations live in
`bot_bottle/contrib/<provider>/` and are imported lazily."""

from __future__ import annotations

from abc import ABC, abstractmethod


class DeployKeyCollisionError(RuntimeError):
    """A deploy key with this title or public key already exists on the repo."""


class DeployKeyProvisioner(ABC):
    """One deploy key's lifecycle on a forge."""

    @abstractmethod
    def create(self, owner_repo: str, title: str) -> tuple[str, bytes]:
        """Generate a keypair and register the public half as a deploy key on
        `owner_repo` (`<owner>/<repo>`) under `title`. Returns `(key_id,
        private_key_bytes)`; `key_id` is only passed back to `delete`."""

    @abstractmethod
    def delete(self, owner_repo: str, key_id: str) -> None:
        """Delete the key. An already-absent key is success; any other failure
        raises so teardown halts."""


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
    from .manifest.util import ManifestError
    raise ManifestError(
        f"unknown provisioned_key provider: {provider!r}; "
        f"available: gitea"
    )
