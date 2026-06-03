"""Unit: deploy_key_provisioner factory (PRD 0048)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from bot_bottle.deploy_key_provisioner import DeployKeyProvisioner, get_provisioner
from bot_bottle.manifest import ManifestError


class TestGetProvisioner(unittest.TestCase):
    def test_gitea_returns_gitea_provisioner(self):
        from bot_bottle.contrib.gitea.deploy_key_provisioner import (
            GiteaDeployKeyProvisioner,
        )
        p = get_provisioner("gitea", token="tok", api_url="https://gitea.example.com")
        self.assertIsInstance(p, GiteaDeployKeyProvisioner)
        self.assertIsInstance(p, DeployKeyProvisioner)

    def test_unknown_provider_raises_manifest_error(self):
        with self.assertRaises(ManifestError) as ctx:
            get_provisioner("github", token="tok", api_url="https://github.com")
        self.assertIn("github", str(ctx.exception))
        self.assertIn("provisioned_key provider", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
