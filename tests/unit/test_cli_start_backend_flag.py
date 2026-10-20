"""Unit: backend resolution priority — explicit name > settings.

The `--backend` flag has been removed from `cli.py start`; backend
selection is driven by BOT_BOTTLE_BACKEND (via `Settings`) or auto-selection only.
`get_bottle_backend` still accepts an explicit name so that `resume`
(which records the backend from a prior session) and the `backend`
sub-command can pass one directly."""

from __future__ import annotations

import unittest

from bot_bottle.cli.settings import settings_from_env

_FIRECRACKER = settings_from_env({"BOT_BOTTLE_BACKEND": "firecracker"})


class TestBackendResolutionPriority(unittest.TestCase):
    def test_explicit_name_overrides_env_var(self):
        from bot_bottle.backend import get_bottle_backend
        self.assertEqual("docker", get_bottle_backend(_FIRECRACKER, "docker").name)

    def test_env_var_used_when_no_explicit_name(self):
        from bot_bottle.backend import get_bottle_backend
        self.assertEqual("firecracker", get_bottle_backend(_FIRECRACKER).name)


if __name__ == "__main__":
    unittest.main()
