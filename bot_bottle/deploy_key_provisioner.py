"""Deploy-key provisioner interface and factory (PRD 0048).

The core defines the abstract contract; concrete implementations live
in `bot_bottle/contrib/<provider>/deploy_key_provisioner.py`. The
factory `get_provisioner` imports contrib modules lazily so that a
missing optional dependency in one provider doesn't break unrelated
features."""

from __future__ import annotations

from abc import ABC, abstractmethod


class DeployKeyProvisioner(ABC):
    """Manages a single deploy-key lifecycle on a remote forge."""

    @abstractmethod
    def create(self, owner_repo: str, title: str) -> tuple[str, bytes]:
        """Generate a keypair and register the public half as a
        deploy key on the forge.

        `owner_repo` is the `<owner>/<repo>` path (no `.git` suffix).
        `title` is the human-readable label shown in the forge UI.

        Returns `(key_id, private_key_bytes)` where `key_id` is opaque
        to the caller and is only ever passed back to `delete`."""

    @abstractmethod
    def delete(self, owner_repo: str, key_id: str) -> None:
        """Delete the registered deploy key.

        Must not raise if the key is already absent (HTTP 404 is
        success). Must raise for all other failures so teardown halts."""


def get_provisioner(
    provider: str, token: str, api_url: str
) -> DeployKeyProvisioner:
    """Instantiate the contrib provisioner for `provider`.

    Raises `ManifestError` for unknown providers so the error surfaces
    at parse time rather than at runtime."""
    if provider == "gitea":
        from bot_bottle.contrib.gitea.deploy_key_provisioner import (
            GiteaDeployKeyProvisioner,
        )
        return GiteaDeployKeyProvisioner(token=token, api_url=api_url)
    from .manifest_util import ManifestError
    raise ManifestError(
        f"unknown provisioned_key provider: {provider!r}; "
        f"available: gitea"
    )
