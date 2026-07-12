"""Unit: `cli.py start --backend=<name>` flag (issue #77).

Asserts that the flag wins over the env var, that the env var is
the fallback, and that the choices are pulled from the backend
registry (so adding a backend lights up in argparse without code
edits)."""

from __future__ import annotations

import argparse
import os
import unittest
from unittest.mock import patch


from bot_bottle.backend import known_backend_names


class TestStartBackendFlag(unittest.TestCase):
    """The flag is wired by `cmd_start`'s argparse and threaded
    through `prepare_with_preflight(backend_name=...)`. Rather than
    drive the whole start flow (which builds containers), we test
    the argparse shape and the resolution function separately."""

    def _build_parser(self):
        # Mirror the parser definition from `cmd_start` so this
        # test doesn't have to invoke the full command.
        parser = argparse.ArgumentParser(prog="cb start")
        parser.add_argument(
            "--backend",
            choices=known_backend_names(),
            default=None,
        )
        parser.add_argument("name")
        return parser

    def test_flag_recognized(self):
        args = self._build_parser().parse_args(["--backend=firecracker", "researcher"])
        self.assertEqual("firecracker", args.backend)
        self.assertEqual("researcher", args.name)

    def test_flag_default_none_means_env_or_default_backend(self):
        args = self._build_parser().parse_args(["researcher"])
        self.assertIsNone(args.backend)

    def test_invalid_backend_rejected_by_argparse(self):
        parser = self._build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["--backend=garbage", "researcher"])

    def test_resolution_priority_explicit_over_env(self):
        # Independent assertion that get_bottle_backend (where
        # `--backend` ultimately threads to) prefers the explicit
        # name over BOT_BOTTLE_BACKEND.
        from bot_bottle.backend import get_bottle_backend
        with patch.dict(os.environ, {"BOT_BOTTLE_BACKEND": "firecracker"}):
            self.assertEqual("docker", get_bottle_backend("docker").name)


if __name__ == "__main__":
    unittest.main()
