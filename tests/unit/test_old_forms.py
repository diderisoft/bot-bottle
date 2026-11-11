"""Unit: each manifest form that changed is refused at load in one line naming
its new form, and the shipped examples and templates load as written."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.contract.yaml_subset import parse_frontmatter
from bot_bottle.manifest import ManifestError, ManifestIndex
from bot_bottle.manifest.agent import ManifestAgent, agent_from_frontmatter
from bot_bottle.manifest.extends import resolve_bottles
from bot_bottle.manifest.util import OldFormError

ROOT = Path(__file__).resolve().parents[2]
DOCS = "(see docs/reference/manifest.md)"


def _bottle(raw: dict[str, Any]) -> None:
    ManifestIndex.from_json_obj({"bottles": {"dev": raw}, "agents": {}})


def _route(route: dict[str, Any]) -> None:
    _bottle({"egress": {"routes": [{"host": "h.example", **route}]}})


def _agent(raw: dict[str, Any]) -> None:
    ManifestAgent.from_dict("a", raw)


class TestBottleForms(unittest.TestCase):
    def assert_refused(self, load: Any, where: str, change: str) -> None:
        with self.assertRaises(OldFormError) as cm:
            load()
        self.assertEqual(f"{where}: {change} {DOCS}", str(cm.exception))

    def test_each_old_bottle_form(self) -> None:
        route = "bottle 'dev' egress route h.example"
        cases: list[tuple[Any, str, str]] = [
            (lambda: _bottle({"git-gate": {"repos": []}}), "bottle 'dev'",
             "`git-gate` is now `git_gate`"),
            (lambda: _bottle({"egress": {"log": 2}}), "bottle 'dev' egress",
             "`log: 2` is now `log: full`"),
            (lambda: _bottle({"egress": {"log": 1}}), "bottle 'dev' egress",
             "`log: 1` is now `log: blocked`"),
            (lambda: _route({"inspect": False}), route,
             "`inspect: false` is now `passthrough: true`"),
            (lambda: _route({"inspect": {"matches": [{"methods": ["GET"]}]}}), route,
             "`inspect` is gone and `matches` is now `allow`, on the route"),
            (lambda: _route({"inspect": {"outbound_detectors": False}}), route,
             "`inspect`'s detectors are now `dlp: { outbound, inbound, on_match }`, "
             "on the route"),
            (lambda: _route({"inspect": {"git": {"fetch": True}}}), route,
             "`inspect` is gone: its `auth`, `git` and `preserve_auth` now sit on the route"),
            (lambda: _route({"matches": [{"methods": ["GET"]}]}), route,
             "`matches` is now `allow`"),
            (lambda: _route({"role": []}), route, "`role` is gone; remove it"),
            (lambda: _route({"auth": {"scheme": "Bearer", "token_ref": "T"}}), route,
             "`auth.token_ref: NAME` is now `auth.value: secret:NAME`"),
            (lambda: _route({"allow": [{"paths": [{"type": "prefix", "value": "/v1/"}]}]}),
             "bottle 'dev' egress.routes[0] allow[0] paths[0]",
             "`{type: prefix, value: ...}` is now the path itself, a string"),
            (lambda: _route({"allow": [{"paths": [{"type": "regex", "value": "^/x"}]}]}),
             "bottle 'dev' egress.routes[0] allow[0] paths[0]",
             "`{type: regex, value: ...}` is now `{regex: ...}`"),
        ]
        for load, where, change in cases:
            with self.subTest(change=change):
                self.assert_refused(load, where, change)

    def test_each_old_agent_form(self) -> None:
        provider = "agent 'a' agent_provider"
        cases: list[tuple[dict[str, Any], str, str]] = [
            ({"agent_provider": {"harness": "codex", "forward_host_credentials": True}},
             provider, "`forward_host_credentials: true` is now `login: host`"),
            ({"agent_provider": {"auth_token": "T"}}, provider,
             "`auth_token: NAME` is now `login: { oauth_token: secret:NAME }`"),
            ({"agent_provider": {"settings": {"api_key_env": "K"}}}, provider,
             "`settings.api_key_env: NAME` is now `login: { api_key: secret:NAME }`"),
            ({"agent_provider": {"dockerfile": "agent/Dockerfile"}}, provider,
             "`agent_provider.dockerfile` (a path) is now `agent_provider.dockerfile_path`"),
            ({"git-gate": {"user": {"name": "a"}}}, "agent 'a'",
             "`git-gate.user` is now `identity.git`"),
            ({"forge_users": {"gitea.example.com": "bot"}}, "agent 'a'",
             "`forge_users` is now `identity.accounts`"),
        ]
        for raw, where, change in cases:
            with self.subTest(change=change):
                self.assert_refused(lambda: _agent(raw), where,  # pylint: disable=cell-var-from-loop
                                    change)


class TestNewForms(unittest.TestCase):
    def test_a_bottle_in_the_current_shape(self) -> None:
        bottle = resolve_bottles({"dev": {
            "env": {"REGION": "?Which region", "GH": "secret:${GITHUB_PAT}"},
            "git_gate": {"user": {"name": "bot"}},
            "egress": {"log": "blocked", "routes": [
                {"host": "api.example.com", "methods": ["GET", "POST"],
                 "auth": {"type": "token", "value": "secret:API"},
                 "dlp": {"outbound": ["token_patterns", "entropy"], "on_match": "block"}},
                {"host": "files.example.com",
                 "allow": [{"paths": ["/pub/", {"exact": "/index"}]}]},
                {"host": "raw.example.com", "passthrough": True},
            ]}}})["dev"]
        api, files, raw = bottle.egress.routes
        self.assertEqual(1, bottle.egress.Log)
        self.assertEqual("bot", bottle.git_user.name)
        self.assertEqual((("GET", "POST"),), tuple(m.Methods for m in api.Matches))
        self.assertEqual(("token_patterns", "entropy"), api.OutboundDetectors)
        self.assertEqual("block", api.OutboundOnMatch)
        self.assertEqual((("GET", "HEAD"),), tuple(m.Methods for m in files.Matches))
        self.assertEqual([("prefix", "/pub/"), ("exact", "/index")],
                         [(p.Type, p.Value) for p in files.Matches[0].Paths])
        self.assertFalse(raw.Inspect)

    def test_dlp_false_turns_every_detector_off(self) -> None:
        bottle = resolve_bottles({"dev": {"egress": {"routes": [
            {"host": "h.example", "dlp": False}]}}})["dev"]
        route = bottle.egress.routes[0]
        self.assertEqual(((), (), ""), (route.OutboundDetectors, route.InboundDetectors,
                                        route.OutboundOnMatch))

    def test_any_method_stands_alone_and_methods_must_be_methods(self) -> None:
        for methods in (["*", "GET"], [], ["FETCH"], "GET"):
            with self.subTest(methods=methods), self.assertRaises(ManifestError):
                _route({"methods": methods})

    def test_the_model_key_is_warned_about_once(self) -> None:
        with mock.patch("bot_bottle.manifest.agent._MODEL_WARNED", set()), \
                mock.patch("bot_bottle.lib.log.warn") as warn:
            _agent({"model": "opus"})
            _agent({"model": "opus"})
            ManifestAgent.from_dict("bottle-architect", {"model": "haiku"})
        self.assertEqual(1, warn.call_count)
        self.assertIn("model is ignored", warn.call_args.args[0])


class TestShippedFiles(unittest.TestCase):
    def test_example_agents_load(self) -> None:
        # The demo has no agent file of its own: it runs `bot-bottle setup`'s starters.
        files = [*(ROOT / "examples" / "agents").glob("*.md")]
        self.assertTrue(files)
        for path in files:
            with self.subTest(path=path.name):
                fm, body = parse_frontmatter(path.read_text())
                agent_from_frontmatter(path.stem, dict(fm), body)

    def test_example_demo_and_project_bottles_load(self) -> None:
        files = {p.stem: p for p in (ROOT / "examples" / "bottles").glob("*.md")}
        files |= {
            # The demo project's `./main` (scripts/demo/fixture), apart from this repo's.
            "demo-main": ROOT / "scripts/demo/fixture/.bot-bottle/bottles/main.md",
            "main": ROOT / ".bot-bottle/bottles/main.md",
            "basic": ROOT / "bot_bottle/manifest/builtin/basic.md",
        }
        raws = {name: dict(parse_frontmatter(p.read_text())[0]) for name, p in files.items()}
        resolve_bottles(raws)


if __name__ == "__main__":
    unittest.main()
