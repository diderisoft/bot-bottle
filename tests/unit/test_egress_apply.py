"""Unit: validate_routes_content (issue #198: _merge_single_route and
add_route removed; docker exec / cp / kill paths are covered by the
integration test)."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.unit import use_bottle_root
from bot_bottle.backend.egress_apply import EgressApplyError
from bot_bottle.backend.docker.egress_apply import applicator


_ROUTES_EMPTY = "routes: []\n"
_ROUTES_ONE = 'routes:\n  - host: "api.anthropic.com"\n'


class TestValidateRoutesContent(unittest.TestCase):
    def test_accepts_minimal_route_table(self):
        applicator.validate_routes_content(_ROUTES_EMPTY)
        applicator.validate_routes_content(_ROUTES_ONE)

    def test_accepts_full_route_with_matches(self):
        applicator.validate_routes_content(
            'routes:\n'
            '  - host: "api.github.com"\n'
            '    auth_scheme: "Bearer"\n'
            '    token_env: "EGRESS_TOKEN_0"\n'
            '    matches:\n'
            '      - paths:\n'
            '          - value: "/repos/x/"\n'
        )

    def test_rejects_bad_yaml(self):
        with self.assertRaises(EgressApplyError) as cm:
            applicator.validate_routes_content("routes:\n\t- host: x\n")
        self.assertIn("not valid", str(cm.exception))

    def test_rejects_missing_routes_key(self):
        with self.assertRaises(EgressApplyError):
            applicator.validate_routes_content("other: []\n")

    def test_rejects_non_list_routes(self):
        with self.assertRaises(EgressApplyError):
            applicator.validate_routes_content('routes: "not a list"\n')

    def test_rejects_partial_auth_pair(self):
        with self.assertRaises(EgressApplyError):
            applicator.validate_routes_content(
                'routes:\n'
                '  - host: "x.example"\n'
                '    auth_scheme: "Bearer"\n'
            )

    def test_rejects_log_full(self):
        with self.assertRaises(EgressApplyError) as cm:
            applicator.validate_routes_content(
                'log: 2\n'
                'routes:\n'
                '  - host: "x.example"\n'
            )
        self.assertIn("must not change egress logging", str(cm.exception))


class TestApplyRoutesChange(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="egress-apply-test.")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(use_bottle_root(Path(self._tmp.name) / ".bot-bottle"))

    def test_writes_live_routes_and_signals_reload(self):
        calls: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(list(argv))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch(
            "bot_bottle.backend.docker.egress_apply.subprocess.run",
            side_effect=fake_run,
        ):
            before, after = applicator.apply_routes_change(
                "dev",
                "routes:\n  - host: google.com\n",
            )

        self.assertEqual("", before)
        self.assertEqual("routes:\n  - host: google.com\n", after)
        self.assertEqual(
            "routes:\n  - host: google.com\n",
            (Path(self._tmp.name) / ".bot-bottle/state/dev/egress/routes.yaml").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            ["docker", "kill", "--signal", "HUP", "bot-bottle-sidecars-dev"],
            calls[0],
        )


if __name__ == "__main__":
    unittest.main()
