"""Unit: validate_routes_content (issue #198: _merge_single_route and
add_route removed; docker exec / cp / kill paths are covered by the
integration test)."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot_bottle import supervise
from bot_bottle.backend.docker.egress_apply import (
    EgressApplyError,
    apply_routes_change,
    validate_routes_content,
)


_ROUTES_EMPTY = "routes: []\n"
_ROUTES_ONE = 'routes:\n  - host: "api.anthropic.com"\n'


class TestValidateRoutesContent(unittest.TestCase):
    def test_accepts_minimal_route_table(self):
        validate_routes_content(_ROUTES_EMPTY)
        validate_routes_content(_ROUTES_ONE)

    def test_accepts_full_route_with_matches(self):
        validate_routes_content(
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
            validate_routes_content("routes:\n\t- host: x\n")
        self.assertIn("not valid", str(cm.exception))

    def test_rejects_missing_routes_key(self):
        with self.assertRaises(EgressApplyError):
            validate_routes_content("other: []\n")

    def test_rejects_non_list_routes(self):
        with self.assertRaises(EgressApplyError):
            validate_routes_content('routes: "not a list"\n')

    def test_rejects_partial_auth_pair(self):
        with self.assertRaises(EgressApplyError):
            validate_routes_content(
                'routes:\n'
                '  - host: "x.example"\n'
                '    auth_scheme: "Bearer"\n'
            )


class TestApplyRoutesChange(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="egress-apply-test.")
        original = supervise.bot_bottle_root

        def fake_root() -> Path:
            return Path(self._tmp.name) / ".bot-bottle"

        supervise.bot_bottle_root = fake_root  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(supervise, "bot_bottle_root", original))
        self.addCleanup(self._tmp.cleanup)

    def test_writes_live_routes_and_signals_reload(self):
        calls: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(list(argv))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch(
            "bot_bottle.backend.docker.egress_apply.subprocess.run",
            side_effect=fake_run,
        ):
            before, after = apply_routes_change(
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
