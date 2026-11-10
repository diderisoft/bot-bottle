"""Unit: where a secret value comes from (`?`, `${VAR}`, `secret:NAME`), for
env entries and a route's `inspect.auth.token`: resolving each form with and
without someone to ask, the compatibility fallbacks and their warnings, the
save offer, the `token_ref` alias, trust, the push scan's held values and the
writers."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bot_bottle import secret_ref
from bot_bottle.backend.launch_common import held_secret_values
from bot_bottle.backend.plan import EgressTokenSecrets
from bot_bottle.contract.yaml_subset import parse_frontmatter, serialize_yaml_subset
from bot_bottle.control.hostctl.audit_events import digest_request
from bot_bottle.egress import Egress
from bot_bottle import env as env_module
from bot_bottle.env import _confirm_save, resolve_env  # pylint: disable=protected-access
from bot_bottle.errors import MissingEnvVarError
from bot_bottle.manifest import Manifest, ManifestError, ManifestIndex
from bot_bottle.manifest.document import bottle_document, route_document
from bot_bottle.manifest.egress import ManifestEgressRoute
from bot_bottle.manifest.util import OldFormError
from bot_bottle.manifest.extends import resolve_bottles
from bot_bottle.secret_source import FileSecrets, LaunchSecrets, MappingSecrets
from bot_bottle.suggest.proposal import (
    OPTION_NEW, Proposal, ProposalAdditions, ProposalCredential, ProposalEgress, ProposalEnv,
    ProposalNewBottle,
)
from bot_bottle.suggest.validate import proposal_to_frontmatter


def _manifest(env: dict[str, str], routes: list[dict[str, Any]] | None = None) -> Manifest:
    bottle: dict[str, Any] = {"env": env}
    if routes is not None:
        bottle["egress"] = {"routes": routes}
    return ManifestIndex.from_json_obj({
        "bottles": {"dev": bottle},
        "agents": {"demo": {}},
    }).load_for_agent("demo", ("dev",))


def _auth_route(auth: dict[str, Any]) -> dict[str, Any]:
    return {"host": "api.example.com", "auth": {"type": "bearer", **auth}}


class _Store(unittest.TestCase):
    """A secrets file in a temp dir, and the prompt/log seams patched."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="secret-syntax."))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = self.dir / "secrets"
        self.ask = self.enterContext(mock.patch("bot_bottle.env._read_secret_silent"))
        self.shown = self.enterContext(mock.patch("bot_bottle.env._read_shown"))
        # A terminal is there to ask; `TestNoOneToAsk` takes it away.
        self.enterContext(mock.patch("bot_bottle.env.can_ask", return_value=True))
        self.save = self.enterContext(mock.patch("bot_bottle.env._confirm_save",
                                                 return_value=True))
        self.warn = self.enterContext(mock.patch("bot_bottle.env.warn"))
        self.info = self.enterContext(mock.patch("bot_bottle.env.info"))

    def store(self, **values: str) -> FileSecrets:
        self.path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
        self.path.chmod(0o600)
        return FileSecrets(self.path)

    def secrets(self, env: dict[str, str] | None = None, answers: dict[str, str] | None = None,
                **stored: str) -> LaunchSecrets:
        return LaunchSecrets(MappingSecrets(env or {}), self.store(**stored), answers)

    def warned(self) -> list[str]:
        return [c.args[0] for c in self.warn.call_args_list]


class TestKinds(unittest.TestCase):
    def test_each_form(self) -> None:
        self.assertEqual(secret_ref.PROMPT, secret_ref.kind("?"))
        self.assertEqual(secret_ref.PROMPT, secret_ref.kind("?Paste your npm token"))
        self.assertEqual(secret_ref.HOST_ENV, secret_ref.kind("${GITHUB_PAT}"))
        self.assertEqual(secret_ref.STORED, secret_ref.kind("secret:OPENAI_API_KEY"))
        self.assertEqual(secret_ref.LITERAL, secret_ref.kind("plain"))
        self.assertEqual(secret_ref.LITERAL, secret_ref.kind("a${B}"))
        self.assertEqual("OPENAI_API_KEY", secret_ref.stored_name("secret:OPENAI_API_KEY"))
        self.assertEqual("GITHUB_PAT", secret_ref.host_var("${GITHUB_PAT}"))

    def test_where_each_comes_from(self) -> None:
        self.assertEqual("asks", secret_ref.source_label("?msg"))
        self.assertEqual("env GITHUB_PAT", secret_ref.source_label("${GITHUB_PAT}"))
        self.assertEqual("secrets file K", secret_ref.source_label("secret:K"))
        self.assertEqual("", secret_ref.source_label("lit"))

    def test_invalid_stored_names_are_refused_at_load(self) -> None:
        for bad in ("secret:", "secret:a b", "secret:1X", "secret:A-B"):
            with self.subTest(bad), self.assertRaisesRegex(ManifestError, "secret:NAME"):
                _manifest({"K": bad})
        self.assertEqual("secret:A_1", _manifest({"K": "secret:A_1"}).bottle.env["K"])

    def test_the_documented_example_loads(self) -> None:
        text = ("---\nenv:\n  NPM_TOKEN: ?Paste your npm token\n  GH_TOKEN: ${GITHUB_PAT}\n"
                "  OPENAI_API_KEY: secret:OPENAI_API_KEY\negress:\n  routes:\n"
                "    - host: api.example.com\n"
                "      auth: { type: bearer, value: secret:EXAMPLE_API_TOKEN }\n---\n")
        bottle = resolve_bottles({"doc": parse_frontmatter(text)[0]})["doc"]
        self.assertEqual("EXAMPLE_API_TOKEN", bottle.egress.routes[0].TokenRef)
        self.assertEqual({"NPM_TOKEN": "?Paste your npm token", "GH_TOKEN": "${GITHUB_PAT}",
                          "OPENAI_API_KEY": "secret:OPENAI_API_KEY"}, dict(bottle.env))


class TestPrompt(_Store):
    def test_interactive_asks_every_launch_and_hints_at_a_saved_value(self) -> None:
        self.ask.return_value = "typed"
        resolved = resolve_env(_manifest({"FOO": "secret:?Paste FOO"}),
                               self.secrets(FOO="saved"))
        self.assertEqual({"FOO": "typed"}, resolved.forwarded)
        self.ask.assert_called_once_with("FOO", "Paste FOO")
        self.info.assert_called_once_with(
            "(a saved FOO exists; write FOO: secret:FOO to use it without asking)")
        self.assertEqual([], self.warned())

    def test_a_plain_question_is_asked_shown_not_hidden(self) -> None:
        self.shown.return_value = "eu-west-1"
        resolved = resolve_env(_manifest({"REGION": "?Which region"}), self.secrets())
        self.assertEqual({"REGION": "eu-west-1"}, resolved.forwarded)
        self.shown.assert_called_once_with("REGION", "Which region")
        self.ask.assert_not_called()

    def test_interactive_without_a_saved_value_asks_without_a_hint(self) -> None:
        self.ask.return_value = "typed"
        resolve_env(_manifest({"FOO": "secret:?"}), self.secrets())
        self.ask.assert_called_once_with("FOO", "")
        self.info.assert_not_called()

    def test_an_answer_typed_on_the_cli_is_used_without_asking_again(self) -> None:
        resolved = resolve_env(_manifest({"FOO": "secret:?"}),
                               self.secrets(answers={"FOO": "typed"}, FOO="saved"), prompt=False)
        self.assertEqual({"FOO": "typed"}, resolved.forwarded)
        self.ask.assert_not_called()
        self.assertEqual([], self.warned())

    def test_no_one_to_ask_uses_the_saved_value_with_a_warning(self) -> None:
        for raw, asks in (("?", "?"), ("secret:?", "secret:?")):
            self.warn.reset_mock()
            resolved = resolve_env(_manifest({"FOO": raw}), self.secrets(FOO="saved"),
                                   prompt=False)
            self.assertEqual({"FOO": "saved"}, resolved.forwarded)
            self.assertEqual([f"FOO: {asks} asks at every launch; there's no one to ask here, "
                              "so the saved value is used. Write FOO: secret:FOO to say so."],
                             self.warned())
        self.ask.assert_not_called()
        self.shown.assert_not_called()

    def test_no_one_to_ask_uses_the_environments_value_with_a_warning(self) -> None:
        resolved = resolve_env(_manifest({"FOO": "secret:?"}), self.secrets(env={"FOO": "env"}),
                               prompt=False)
        self.assertEqual({"FOO": "env"}, resolved.forwarded)
        self.assertEqual(["FOO: secret:? asks at every launch; there's no one to ask here, so "
                          "the value in the environment is used. Write FOO: secret:${FOO} to "
                          "say so."], self.warned())

    def test_no_one_to_ask_and_nothing_saved_fails(self) -> None:
        with self.assertRaises(MissingEnvVarError) as cm:
            resolve_env(_manifest({"FOO": "?"}), self.secrets(), prompt=False)
        self.assertEqual("FOO", cm.exception.var_name)
        self.assertIn("prompting is not allowed", str(cm.exception))


class TestNoOneToAsk(unittest.TestCase):
    """An interactive launch with nothing to ask through (CI: stdin a pipe,
    no controlling terminal) takes the no-one-to-ask path, never dies on
    /dev/tty."""

    def setUp(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle import env
        self.env = env
        self.dir = Path(tempfile.mkdtemp(prefix="secret-syntax."))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.enterContext(mock.patch.object(env, "current_prompter", return_value=None))
        self.tty = self.enterContext(mock.patch.object(env, "_open_tty", return_value=None))
        self.warn = self.enterContext(mock.patch.object(env, "warn"))
        self.info = self.enterContext(mock.patch.object(env, "info"))
        self.die = self.enterContext(mock.patch.object(env, "die", side_effect=SystemExit))

    def secrets(self, **stored: str) -> LaunchSecrets:
        path = self.dir / "secrets"
        path.write_text("".join(f"{k}={v}\n" for k, v in stored.items()))
        path.chmod(0o600)
        return LaunchSecrets(MappingSecrets({}), FileSecrets(path))

    def terminals(self, *, stdin: bool, stderr: bool) -> None:
        self.enterContext(mock.patch.object(self.env.sys, "stdin", mock.Mock(
            isatty=mock.Mock(return_value=stdin))))
        self.enterContext(mock.patch.object(self.env.sys, "stderr", mock.Mock(
            isatty=mock.Mock(return_value=stderr))))

    def test_without_a_tty_to_read_a_question_falls_back_whatever_stderr_is(self) -> None:
        for stderr in (True, False):
            for raw in ("?", "secret:?Paste it"):
                with self.subTest(stderr=stderr, raw=raw):
                    self.terminals(stdin=False, stderr=stderr)
                    self.assertFalse(self.env.can_ask())
                    self.warn.reset_mock()
                    resolved = resolve_env(_manifest({"TEST_HELD_SECRET": raw}),
                                           self.secrets(TEST_HELD_SECRET="saved-value"))
                    self.assertEqual({"TEST_HELD_SECRET": "saved-value"}, resolved.forwarded)
                    self.assertIn("there's no one to ask here",
                                  self.warn.call_args.args[0])
        self.info.assert_not_called()  # no "a saved ... exists" hint
        self.die.assert_not_called()

    def test_a_missing_stored_secret_fails_with_the_file_hint(self) -> None:
        self.terminals(stdin=False, stderr=True)
        with self.assertRaises(MissingEnvVarError) as cm:
            resolve_env(_manifest({"A": "secret:NAME"}), self.secrets())
        self.assertIn("NAME=<value>", str(cm.exception))
        self.die.assert_not_called()

    def test_the_cli_asks_nothing(self) -> None:
        self.terminals(stdin=False, stderr=True)
        self.assertEqual({}, self.env.ask_env(_manifest({"A": "?", "B": "secret:NAME"}),
                                              self.secrets()))

    def test_a_terminal_on_stdin_or_dev_tty_can_ask(self) -> None:
        self.terminals(stdin=True, stderr=False)
        self.assertTrue(self.env.can_ask())
        self.terminals(stdin=False, stderr=False)
        self.tty.return_value = mock.MagicMock()
        self.assertTrue(self.env.can_ask())


class TestHostEnv(_Store):
    def test_reads_the_environment(self) -> None:
        resolved = resolve_env(_manifest({"GH_TOKEN": "${GITHUB_PAT}"}),
                               self.secrets(env={"GITHUB_PAT": "env"}, GITHUB_PAT="file"))
        self.assertEqual({"GH_TOKEN": "env"}, resolved.forwarded)
        self.assertEqual([], self.warned())
        self.ask.assert_not_called()

    def test_falls_back_to_the_secrets_file_with_a_warning(self) -> None:
        for prompt in (True, False):
            self.warn.reset_mock()
            resolved = resolve_env(_manifest({"GH_TOKEN": "${GITHUB_PAT}"}),
                                   self.secrets(GITHUB_PAT="file"), prompt=prompt)
            self.assertEqual({"GH_TOKEN": "file"}, resolved.forwarded)
            self.assertEqual(["GH_TOKEN: ${GITHUB_PAT} read GITHUB_PAT from the secrets file; "
                              "write secret:GITHUB_PAT for that."], self.warned())
        self.ask.assert_not_called()

    def test_a_secret_env_value_reads_the_same_and_says_so_in_its_warning(self) -> None:
        resolved = resolve_env(_manifest({"GH_TOKEN": "secret:${GITHUB_PAT}"}),
                               self.secrets(GITHUB_PAT="file"), prompt=False)
        self.assertEqual({"GH_TOKEN": "file"}, resolved.forwarded)
        self.assertEqual(["GH_TOKEN: secret:${GITHUB_PAT} read GITHUB_PAT from the secrets file; "
                          "write secret:GITHUB_PAT for that."], self.warned())

    def test_in_neither_fails_naming_the_variable(self) -> None:
        with self.assertRaises(MissingEnvVarError) as cm:
            resolve_env(_manifest({"GH_TOKEN": "${GITHUB_PAT}"}), self.secrets())
        self.assertEqual("GITHUB_PAT", cm.exception.var_name)
        self.ask.assert_not_called()


class TestStored(_Store):
    def test_reads_the_store_not_the_environment(self) -> None:
        resolved = resolve_env(_manifest({"KEY": "secret:OPENAI_API_KEY"}),
                               self.secrets(env={"OPENAI_API_KEY": "env"},
                                            OPENAI_API_KEY="file"))
        self.assertEqual({"KEY": "file"}, resolved.forwarded)
        self.ask.assert_not_called()
        self.assertEqual([], self.warned())

    def test_missing_interactive_asks_once_and_saves_on_yes(self) -> None:
        self.ask.return_value = "typed-value"
        secrets = self.secrets(env={"OPENAI_API_KEY": "env"})
        resolved = resolve_env(_manifest({"A": "secret:OPENAI_API_KEY",
                                          "B": "secret:OPENAI_API_KEY"}), secrets)
        self.assertEqual({"A": "typed-value", "B": "typed-value"}, resolved.forwarded)
        self.ask.assert_called_once()
        self.save.assert_called_once_with("OPENAI_API_KEY")
        self.assertEqual("typed-value", FileSecrets(self.path).get("OPENAI_API_KEY"))
        self.assertEqual(0o600, stat.S_IMODE(os.stat(self.path).st_mode))

    def test_missing_interactive_declined_is_used_but_not_saved(self) -> None:
        self.ask.return_value = "typed-value"
        self.save.return_value = False
        resolved = resolve_env(_manifest({"A": "secret:NAME"}), self.secrets())
        self.assertEqual({"A": "typed-value"}, resolved.forwarded)
        self.assertIsNone(FileSecrets(self.path).get("NAME"))

    def test_missing_non_interactive_fails_naming_it_and_how_to_add_it(self) -> None:
        with self.assertRaises(MissingEnvVarError) as cm:
            resolve_env(_manifest({"A": "secret:NAME"}),
                        self.secrets(env={"NAME": "env"}), prompt=False)
        self.assertEqual("NAME", cm.exception.var_name)
        self.assertIn("NAME=<value>", str(cm.exception))
        self.assertIn(str(self.path), str(cm.exception))
        self.ask.assert_not_called()

    def test_an_unsaved_answer_from_the_cli_is_used(self) -> None:
        resolved = resolve_env(_manifest({"A": "secret:NAME"}),
                               self.secrets(answers={"A": "typed"}), prompt=False)
        self.assertEqual({"A": "typed"}, resolved.forwarded)

    def test_the_save_offer_defaults_to_yes(self) -> None:
        with mock.patch.object(env_module, "current_prompter", return_value=None), \
                mock.patch.object(env_module, "read_tty_line", side_effect=["", "n", "Y"]), \
                mock.patch.object(env_module.sys, "stderr"):
            self.assertEqual([True, False, True], [_confirm_save("N") for _ in range(3)])


class TestRouteAuth(unittest.TestCase):
    def route(self, auth: dict[str, Any]) -> ManifestEgressRoute:
        return _manifest({}, [{"host": "api.example.com", "auth": auth}]
                         ).bottle.egress.routes[0]

    def test_each_type_maps_onto_the_gateways_fields(self) -> None:
        for auth, scheme, header in (
            ({"type": "bearer", "value": "secret:API_TOKEN"}, "Bearer", ""),
            ({"type": "token", "value": "secret:API_TOKEN"}, "token", ""),
            ({"type": "header", "name": "X-Api-Key", "value": "secret:API_TOKEN"},
             "", "x-api-key"),
        ):
            with self.subTest(auth["type"]):
                route = self.route(auth)
                self.assertEqual((scheme, header, "API_TOKEN"),
                                 (route.AuthScheme, route.AuthHeader, route.TokenRef))

    def test_each_error(self) -> None:
        for auth, why in (
            ({"value": "secret:A"}, "auth.type is required: one of bearer, token, header"),
            ({"type": "basic", "value": "secret:A"}, "one of bearer, token, header"),
            ({"type": "bearer"}, "auth.value is required"),
            ({"type": "header", "value": "secret:A"}, "auth.name .*is required"),
            ({"type": "bearer", "name": "X-A", "value": "secret:A"},
             "auth.name is only for type: header"),
            ({"type": "header", "name": "Bad Name", "value": "secret:A"},
             "not a valid HTTP header name"),
            ({"type": "header", "name": "Cookie", "value": "secret:A"}, "is reserved"),
            ({"type": "bearer", "value": "secret:A", "extra": 1}, "unknown key 'extra'"),
            ({}, "auth is empty"),
        ):
            with self.subTest(auth), self.assertRaisesRegex(ManifestError, why):
                self.route(auth)

    def test_the_old_keys_are_refused_naming_the_new_form(self) -> None:
        for auth, new in (({"scheme": "Bearer", "token_ref": "A"},
                           "`auth.token_ref: NAME` is now `auth.value: secret:NAME`"),
                          ({"header": "X-Api-Key", "value": "secret:A"},
                           "`auth.header: X` is now `auth: { type: header, name: X }`"),
                          ({"scheme": "Bearer", "value": "secret:A"},
                           "`auth.scheme: Bearer` is now `auth.type: bearer`")):
            with self.subTest(auth), self.assertRaises(OldFormError) as cm:
                self.route(auth)
            self.assertEqual(f"bottle 'dev' egress route api.example.com: {new} "
                             f"(see docs/reference/manifest.md)", str(cm.exception))

    def test_a_secret_env_value_is_taken(self) -> None:
        route = self.route({"type": "bearer", "value": "secret:${GH_PAT}"})
        self.assertEqual(("GH_PAT", True), (route.TokenRef, route.TokenFromEnv))
        self.assertEqual({"type": "bearer", "value": "secret:${GH_PAT}"},
                         route_document(route)["auth"])  # type: ignore[index]

    def test_only_secret_name_is_a_value(self) -> None:
        with self.assertRaisesRegex(ManifestError, "secret:\\? isn't taken here"):
            self.route({"type": "bearer", "value": "secret:?Paste it"})
        for bad in ("?Paste it", "${API_TOKEN}", "hunter2-literal"):
            with self.subTest(bad), \
                    self.assertRaisesRegex(ManifestError, "auth.value takes secret:NAME") as cm:
                self.route({"type": "bearer", "value": bad})
            self.assertNotIn("hunter2", str(cm.exception))
        for bad in ("secret:", "secret:a b"):
            with self.subTest(bad), self.assertRaisesRegex(ManifestError, "secret:NAME"):
                self.route({"type": "bearer", "value": bad})

    def test_writers_emit_type_and_value(self) -> None:
        for auth in ({"type": "bearer", "value": "secret:A"},
                     {"type": "token", "value": "secret:A"},
                     {"type": "header", "name": "x-api-key", "value": "secret:A"}):
            with self.subTest(auth["type"]):
                self.assertEqual(auth, route_document(self.route(auth))["auth"])  # type: ignore[index]
        made = ManifestEgressRoute(Host="h.example", AuthScheme="Bearer", TokenRef="B")
        self.assertEqual({"type": "bearer", "value": "secret:B"},
                         route_document(made)["auth"])  # type: ignore[index]

    def test_a_bottle_document_round_trips(self) -> None:
        bottle = _manifest({"K": "secret:K"}, [
            _auth_route({"value": "secret:A"}),
            {"host": "b.example.com", "auth": {
                "type": "header", "name": "X-Key", "value": "secret:B"}},
        ]).bottle
        text = serialize_yaml_subset(bottle_document(bottle))
        self.assertIn("value: secret:A", text)
        self.assertIn("name: x-key", text)
        self.assertNotIn("token_ref", text)
        self.assertNotIn("scheme", text)
        self.assertIn("K: secret:K", text)
        again = resolve_bottles({"doc": parse_frontmatter(f"---\n{text}---\n")[0]})["doc"]
        self.assertEqual(bottle.egress.routes, again.egress.routes)


class TestRouteTokenResolution(unittest.TestCase):
    def setUp(self) -> None:
        self.warn = self.enterContext(mock.patch("bot_bottle.backend.plan.warn"))

    def source(self, provisioned: dict[str, str], env: dict[str, str],
               store: dict[str, str]) -> EgressTokenSecrets:
        return EgressTokenSecrets(
            MappingSecrets(provisioned),
            LaunchSecrets(MappingSecrets(env), MappingSecrets(store)),
            frozenset({"ROUTE_TOK"}))

    def resolve(self, source: EgressTokenSecrets, ref: str = "ROUTE_TOK") -> dict[str, str]:
        return Egress().resolve_token_values({"EGRESS_TOKEN_0": ref}, source)

    def test_provisioned_then_the_store(self) -> None:
        self.assertEqual({"EGRESS_TOKEN_0": "prov"}, self.resolve(
            self.source({"ROUTE_TOK": "prov"}, {"ROUTE_TOK": "env"}, {"ROUTE_TOK": "file"})))
        self.assertEqual({"EGRESS_TOKEN_0": "file"}, self.resolve(
            self.source({}, {"ROUTE_TOK": "env"}, {"ROUTE_TOK": "file"})))
        self.warn.assert_not_called()

    def test_only_in_the_cli_env_is_used_with_a_warning(self) -> None:
        self.assertEqual({"EGRESS_TOKEN_0": "env"}, self.resolve(
            self.source({}, {"ROUTE_TOK": "env"}, {})))
        self.warn.assert_called_once()
        self.assertIn("add ROUTE_TOK there", self.warn.call_args.args[0])

    def test_nowhere_is_an_error(self) -> None:
        with self.assertRaises(MissingEnvVarError):
            self.resolve(self.source({}, {}, {}))

    def test_a_secret_env_route_value_reads_the_environment_first(self) -> None:
        source = EgressTokenSecrets(
            MappingSecrets({}),
            LaunchSecrets(MappingSecrets({"GH": "env"}), MappingSecrets({"GH": "file"})),
            frozenset(), frozenset({"GH"}))
        self.assertEqual({"EGRESS_TOKEN_0": "env"}, self.resolve(source, "GH"))
        self.warn.assert_not_called()
        source = EgressTokenSecrets(
            MappingSecrets({}), LaunchSecrets(MappingSecrets({}), MappingSecrets({"GH": "file"})),
            frozenset(), frozenset({"GH"}))
        self.assertEqual({"EGRESS_TOKEN_0": "file"}, self.resolve(source, "GH"))
        self.assertIn("route secret:${GH} read GH from the secrets file",
                      self.warn.call_args.args[0])

    def test_a_providers_token_resolves_as_before(self) -> None:
        # Not one of the bottle's routes: env, then store, no warning.
        self.assertEqual({"EGRESS_TOKEN_0": "env"}, self.resolve(
            self.source({}, {"PROVIDER_KEY": "env"}, {"PROVIDER_KEY": "file"}), "PROVIDER_KEY"))
        self.warn.assert_not_called()


class TestHeldForThePushScan(unittest.TestCase):
    def test_only_secret_marked_values_are_held_and_short_ones_are_not(self) -> None:
        manifest = _manifest({"ASKED": "?", "FROM_ENV": "${GITHUB_PAT}", "STORED": "secret:K",
                              "HIDDEN": "secret:?", "SECRET_ENV": "secret:${GH}",
                              "SHORT": "secret:S", "PLAIN": "literal-value-long"})
        plan = SimpleNamespace(
            egress_plan=SimpleNamespace(canary="", canary_env=""), manifest=manifest,
            forwarded_env={"ASKED": "asked-value-1", "FROM_ENV": "env-value-12",
                           "STORED": "stored-value-1", "HIDDEN": "hidden-value-1",
                           "SECRET_ENV": "secret-env-value", "SHORT": "1234"})
        self.assertEqual({"HELD_ENV_STORED": "stored-value-1", "HELD_ENV_HIDDEN": "hidden-value-1",
                          "HELD_ENV_SECRET_ENV": "secret-env-value"},
                         held_secret_values(plan))  # type: ignore[arg-type]


class TestMasked(unittest.TestCase):
    def test_a_running_bottles_held_secrets_are_masked_in_transcripts(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.control.hostctl import transcript_serve
        with mock.patch.object(transcript_serve, "host_secrets_path",
                               return_value=Path("/nonexistent/secrets")):
            with transcript_serve.masking(["typed-secret-value-1", "short"]):
                values = set(transcript_serve._secret_values().values())  # pylint: disable=protected-access
                self.assertIn("typed-secret-value-1", values)
                self.assertNotIn("short", values)
                self.assertNotIn("typed-secret-value-1", transcript_serve.mask(
                    "the key is typed-secret-value-1 ok", transcript_serve._secret_values()))  # pylint: disable=protected-access
            self.assertEqual({}, transcript_serve._secret_values())  # pylint: disable=protected-access


class TestUnmarkedWarning(unittest.TestCase):
    def test_a_secret_looking_name_without_secret_is_warned_about(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.manifest import bottle as bottle_module
        with mock.patch.object(bottle_module, "_WARNED", set()), \
                mock.patch("bot_bottle.lib.log.warn") as warn:
            _manifest({"API_KEY": "?", "GH_PAT": "${X}", "REGION": "?", "OK_TOKEN": "secret:?",
                       "DB_PASSWORD": "lit"})
        self.assertEqual(
            ["API_KEY isn't marked secret; write secret:? (or secret:${VAR}) to treat it as one.",
             "GH_PAT isn't marked secret; write secret:? (or secret:${VAR}) to treat it as one."],
            [c.args[0] for c in warn.call_args_list])


class TestWriters(unittest.TestCase):
    def test_a_proposal_writes_stored_env_and_token_secret(self) -> None:
        proposal = Proposal(
            option=OPTION_NEW, bottles=[], new_bottle=ProposalNewBottle(name="x", extends="basic"),
            additions=ProposalAdditions(
                egress=[ProposalEgress(host="api.linear.app", methods=["GET"])],
                env=[ProposalEnv(name="FLY_APP")],
                credentials=[ProposalCredential(name="LINEAR_TOKEN", route="api.linear.app")]),
            similar=[], notes=[])
        fm = proposal_to_frontmatter(proposal)
        self.assertEqual({"FLY_APP": "secret:FLY_APP"}, fm["env"])
        self.assertEqual({"type": "bearer", "value": "secret:LINEAR_TOKEN"},
                         fm["egress"]["routes"][0]["auth"])


class TestAnswersStayOutOfTheAudit(unittest.TestCase):
    def test_the_digest_keeps_names_not_values(self) -> None:
        with_value = digest_request({"agent": "a", "answers": {"FOO": "typed-1"}})
        other_value = digest_request({"agent": "a", "answers": {"FOO": "typed-2"}})
        self.assertEqual(with_value, other_value)


if __name__ == "__main__":
    unittest.main()
