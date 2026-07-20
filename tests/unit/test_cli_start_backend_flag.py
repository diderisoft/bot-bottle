"""Unit: backend resolution priority — explicit name > env var.

The `--backend` flag has been removed from `cli.py start`; backend
selection is driven by BOT_BOTTLE_BACKEND or auto-selection only.
`get_bottle_backend` still accepts an explicit name so that `resume`
(which records the backend from a prior session) and the `backend`
sub-command can pass one directly."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch


class TestBackendResolutionPriority(unittest.TestCase):
    def test_explicit_name_overrides_env_var(self):
        from bot_bottle.backend import get_bottle_backend
        with patch.dict(os.environ, {"BOT_BOTTLE_BACKEND": "firecracker"}):
            self.assertEqual("docker", get_bottle_backend("docker").name)

    def test_env_var_used_when_no_explicit_name(self):
        from bot_bottle.backend import get_bottle_backend
        with patch.dict(os.environ, {"BOT_BOTTLE_BACKEND": "firecracker"}):
            self.assertEqual("firecracker", get_bottle_backend().name)


if __name__ == "__main__":
    unittest.main()
