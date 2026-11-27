"""The bottle architect's settings (PRD 0134): `bottle-architect.host` and
`.connection`, their values, which apply here (the account's while paired,
the host's own while not), what each connection runs on, the console's
`architect` message and the host's `architect.set` (config set, adoption),
the report, `bot-bottle config` and `status`, and the old setup copies of
`agents/bottle-architect.md`."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.control.hostctl import architect_setting
from bot_bottle.lib import architect_values as av
from bot_bottle.lib.config_store import ArchitectValues, ConfigStore, HostConfig
from bot_bottle.secret_source import write_secret
from bot_bottle.suggest import architect_provider as ap
from bot_bottle.suggest.architect_agent import ARCHITECT_AGENT, ARCHITECT_NOTE, adoptable_agents

_OWN_ID = architect_setting._own_id  # pylint: disable=protected-access
ME = "6f1c2a9e-0b7d-4c1e-9a55-2d3f4e5a6b7c"
OTHER = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _only(*harnesses: str) -> Any:
    """A `harness_ready` stand-in: only `harnesses` are ready."""
    def ready(harness: str) -> bool:
        return harness in harnesses
    return ready


class Values(unittest.TestCase):
    def test_connections(self) -> None:
        for value in ("auto", "claude", "codex", "pi:openrouter/qwen/qwen3-coder:free",
                      "pi:groq/llama-3.3-70b", "pi:my.prov_1/org/model@v2+x"):
            with self.subTest(value=value):
                self.assertTrue(av.valid_connection(value))
                self.assertEqual(value, av.check_connection(value))
        for value in ("", "off", "local", "Auto", "hosted", "pi:", "pi:openrouter", "pi:/m",
                      "pi:a/", "pi:a b/m", "claude\n", "pi:a/" + "m" * 200, None, 3):
            with self.subTest(value=value):
                self.assertFalse(av.valid_connection(value))
                with self.assertRaisesRegex(ValueError, "auto, claude, codex or pi:"):
                    av.check_connection(value)

    def test_hosts(self) -> None:
        for value in (ME, "delphi", "h1", "a" * 200):
            with self.subTest(value=value):
                self.assertTrue(av.valid_account_host(value))
                self.assertFalse(av.valid_local_host(value))
        self.assertTrue(av.valid_account_host("off"))
        for value in ("", "local", "-x", "a b", "a/b", "x\n", "a" * 201, None, 3):
            with self.subTest(value=value):
                self.assertFalse(av.valid_account_host(value))
                with self.assertRaisesRegex(ValueError, "a host's id, local"):
                    av.check_account_host(value)
        self.assertEqual(["local", "off"], [v for v in ("local", "off", ME, "")
                                            if av.valid_local_host(v)])
        with self.assertRaisesRegex(ValueError, "local .this host. or off"):
            av.check_local_host(ME)

    def test_this_hosts_own_are_validated_where_theyre_set(self) -> None:
        self.assertEqual(("local", "auto"), (HostConfig().architect_host,
                                             HostConfig().architect_connection))
        with self.assertRaisesRegex(ValueError, "bottle-architect.host"):
            HostConfig().with_json("architect_host", ME)
        with self.assertRaisesRegex(ValueError, "bottle-architect.connection"):
            HostConfig().with_json("architect_connection", "off")
        config = HostConfig(architect_host="off", architect_connection="codex")
        self.assertEqual(config, HostConfig.from_json(config.to_json()))
        self.assertEqual(("off", "codex"), (config.to_json()["architect_host"],
                                            config.to_json()["architect_connection"]))


class _Store(unittest.TestCase):
    """A host DB, and this host paired as `ME` (`own_id.return_value = None`
    unpairs it)."""

    def setUp(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(tmp)
        self.store = ConfigStore(self.root / "db" / "bot-bottle.db")
        self.store.migrate()
        self.own_id = self.enterContext(
            mock.patch.object(architect_setting, "_own_id", return_value=ME))
        self.enterContext(mock.patch.object(architect_setting, "_store", return_value=self.store))

    def account(self, host: object, connection: object = "auto", **extra: object) -> Any:
        return architect_setting.received({"type": "architect", "host": host,
                                           "connection": connection, **extra}, self.store)

    def found(self) -> architect_setting.InForce:
        return architect_setting.in_force(self.store)


class InForce(_Store):
    def test_paired_its_own_id_is_the_architect(self) -> None:
        self.account(ME, "codex")
        self.assertEqual(architect_setting.InForce(ME, "codex", True), self.found())

    def test_paired_off_is_the_rules(self) -> None:
        self.account("off")
        self.assertEqual(architect_setting.InForce("off", "auto", False,
                                                   "the bottle architect is off"), self.found())

    def test_paired_none_declared_says_how_to_choose(self) -> None:
        for host in ("", None, "local", "bad id"):  # nothing, or nothing valid
            with self.subTest(host=host):
                self.account(host)
                found = self.found()
                self.assertEqual(("", False), (found.host, found.is_architect))
                self.assertEqual("No architect host: choose one in Settings, or "
                                 "`bot-bottle config set bottle-architect.host <host>`.",
                                 found.reason)
        # Never told: the same.
        self.store.set_architect_account(ArchitectValues("", "auto"))
        self.assertEqual("", self.found().host)

    def test_paired_another_host_is_named_by_its_label_else_its_id(self) -> None:
        self.account(OTHER)
        found = self.found()
        self.assertEqual((OTHER, False, OTHER), (found.host, found.is_architect, found.label))
        self.assertEqual(f"The architect host is {OTHER}; suggestions from other hosts reach "
                         "it once routing ships.", found.reason)
        self.account(OTHER, label="delphi")
        self.assertEqual("The architect host is delphi; suggestions from other hosts reach "
                         "it once routing ships.", self.found().reason)

    def test_paired_this_hosts_own_values_dont_apply(self) -> None:
        self.store.set_host_config(HostConfig(architect_host="off",
                                              architect_connection="codex"))
        self.account(ME, "claude")
        self.assertEqual(architect_setting.InForce(ME, "claude", True), self.found())

    def test_unpaired_local_then_off(self) -> None:
        self.own_id.return_value = None
        self.account(OTHER)  # the account's, kept from before: ignored unpaired
        self.assertEqual(architect_setting.InForce("local", "auto", True), self.found())
        self.store.set_host_config(HostConfig(architect_host="off",
                                              architect_connection="codex"))
        self.assertEqual(architect_setting.InForce("off", "codex", False,
                                                   "the bottle architect is off"), self.found())

    def test_an_invalid_connection_is_auto(self) -> None:
        for connection in ("off", 7, None, "gpt"):
            with self.subTest(connection=connection):
                self.account(ME, "codex")
                self.account(ME, connection)
                self.assertEqual("auto", self.found().connection)

    def test_kept_across_a_restart(self) -> None:
        self.account(OTHER, "codex", label="delphi")
        again = ConfigStore(self.store.db_path)
        self.assertEqual((ArchitectValues("local", "auto"),
                          ArchitectValues(OTHER, "codex", "delphi")), again.architect())

    def test_a_db_failure_is_the_defaults_and_logged(self) -> None:
        broken = mock.Mock(spec=ConfigStore)
        broken.architect.side_effect = sqlite3.OperationalError("x")
        with mock.patch.object(architect_setting.log, "warn") as warn:
            self.assertEqual("", architect_setting.in_force(broken).host)
            self.assertEqual((False, None), architect_setting.received(
                {"host": ME}, broken))
        self.assertEqual(2, warn.call_count)

    def test_own_id_is_a_complete_pairings_host_id(self) -> None:
        from bot_bottle.control.hostctl.pairing import Pairing  # pylint: disable=import-outside-toplevel
        with mock.patch("bot_bottle.control.hostctl.pairing.load_pairing",
                        return_value=Pairing(host_id=ME)):
            self.assertIsNone(_OWN_ID())
        with mock.patch("bot_bottle.control.hostctl.pairing.load_pairing",
                        return_value=Pairing(url="https://c", access_token_secret="T",
                                             host_id=ME)):
            self.assertEqual(ME, _OWN_ID())


class Reporting(_Store):
    def ready(self, *harnesses: str) -> Any:
        return mock.patch("bot_bottle.agent_provider.harness_ready",
                          side_effect=_only(*harnesses))

    def test_the_architect_host(self) -> None:
        self.account(ME)
        with self.ready("claude"):
            self.assertEqual({"host": ME, "connection": "auto", "is_architect": True,
                              "ready": True, "runs_on": "claude"},
                             architect_setting.report(self.store))
            self.assertEqual("this host, auto → claude", architect_setting.status()["label"])
        with self.ready():
            report = architect_setting.report(self.store)
        self.assertEqual((True, False), (report["is_architect"], report["ready"]))
        self.assertIn("neither claude nor codex", report["reason"])

    def test_not_the_architect_host(self) -> None:
        for host, label in (("off", "off"), ("", "none"), (OTHER, OTHER)):
            with self.subTest(host=host):
                self.account(host, "codex")
                status = architect_setting.status()
                self.assertEqual({"host": host, "connection": "codex", "is_architect": False,
                                  "ready": False, "label": label}, {
                                      k: v for k, v in status.items() if k != "reason"})
                self.assertEqual(self.found().reason, status["reason"])

    def test_host_stats_carries_the_report_beside_about(self) -> None:
        from bot_bottle.control.hostctl import host_stats  # pylint: disable=import-outside-toplevel
        self.own_id.return_value = None
        with mock.patch.object(host_stats, "collect", return_value={}), \
             mock.patch.object(host_stats, "about", return_value={"os": "macOS"}), \
             mock.patch.object(host_stats.budget, "host_budget"), \
             mock.patch.object(host_stats.budget, "block", return_value={}), \
             self.ready("codex"):
            stats = host_stats.snapshot()
        self.assertEqual({"host": "local", "connection": "auto", "is_architect": True,
                          "ready": True, "runs_on": "codex"}, stats["architect"])
        self.assertNotIn("architect", stats["about"])  # type: ignore[operator]

    def test_status_is_a_cli_route_both_ways(self) -> None:
        # pylint: disable=import-outside-toplevel,protected-access
        from bot_bottle.contract.hostctl import ARCHITECT_PATH
        from bot_bottle.control.hostctl import server
        self.assertIs(architect_setting.status, server._HOST_STATE_READS[ARCHITECT_PATH])
        self.assertIs(architect_setting.change, server._HOST_STATE[ARCHITECT_PATH])

    def test_host_config_has_this_hosts_own_values(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.control.hostctl import host_state
        self.account(OTHER, "codex")
        with mock.patch.object(host_state, "host_db_path", return_value=self.store.db_path):
            config = host_state.config()
        self.assertEqual(("local", "auto"), (config["architect_host"],
                                             config["architect_connection"]))
        self.assertNotIn("architect_provider", config)


class Resolving(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home, self.root = tmp / "home", tmp / "home" / ".bot-bottle"
        self.root.mkdir(parents=True)
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(self.home),
                                                        "BOT_BOTTLE_ROOT": str(self.root)}))

    def ready(self, *harnesses: str) -> Any:
        return mock.patch("bot_bottle.agent_provider.harness_ready",
                          side_effect=_only(*harnesses))

    def test_claude_and_codex_use_the_hosts_login(self) -> None:
        for harness in ("claude", "codex"):
            with self.subTest(harness=harness), self.ready(harness):
                got = ap.resolve(harness)
                self.assertTrue(got.ready)
                self.assertEqual((harness, {"harness": harness, "login": "host"}, harness),
                                 (got.harness, got.agent_provider, got.label))

    def test_a_named_harness_not_ready_is_not_swapped_for_another(self) -> None:
        with self.ready("claude"):
            got = ap.resolve("codex")
        self.assertFalse(got.ready)
        self.assertEqual("", got.harness)
        self.assertEqual("codex isn't installed and logged in on this host", got.reason)

    def test_auto_is_claude_then_codex(self) -> None:
        for ready, harness in ((("claude", "codex"), "claude"), (("codex",), "codex")):
            with self.subTest(ready=ready), self.ready(*ready):
                got = ap.resolve("auto")
                self.assertEqual((True, harness, f"auto → {harness}"),
                                 (got.ready, got.harness, got.label))
        with self.ready():
            got = ap.resolve("auto")
        self.assertEqual((False, "auto"), (got.ready, got.label))
        self.assertIn("neither claude nor codex", got.reason)

    def test_a_bad_value_is_not_ready(self) -> None:
        for value in ("off", "gpt"):  # off is a host value now
            with self.subTest(value=value):
                self.assertIn("isn't a bottle-architect.connection value",
                              ap.resolve(value).reason)

    def pi_config(self, providers: dict[str, Any]) -> None:
        agent = self.home / ".pi" / "agent"
        agent.mkdir(parents=True, exist_ok=True)
        (agent / "models.json").write_text(json.dumps({"providers": providers}))

    def test_pi_uses_the_hosts_config_and_key_for_that_provider(self) -> None:
        self.pi_config({"together": {
            "baseUrl": "https://api.together.xyz/v1", "api": "openai-completions",
            "apiKey": "TOGETHER_API_KEY",
            "models": [{"id": "qwen/coder", "contextWindow": 32768, "maxTokens": 4096}]}})
        write_secret(self.root / "secrets", "TOGETHER_API_KEY", "k")
        got = ap.resolve("pi:together/qwen/coder")
        self.assertTrue(got.ready, got.reason)
        self.assertEqual("pi", got.harness)
        block = got.agent_provider
        self.assertEqual({"api_key": "secret:TOGETHER_API_KEY"}, block["login"])
        settings = block["settings"]
        assert isinstance(settings, dict)
        self.assertEqual(("together", "https://api.together.xyz/v1", ["qwen/coder"], 32768),
                         (settings["provider"], settings["base_url"], settings["models"],
                          settings["context_window"]))
        self.assertNotIn("api_key_env", settings)

    def test_pis_built_in_providers_need_no_config(self) -> None:
        write_secret(self.root / "secrets", "OPENROUTER_API_KEY", "k")
        got = ap.resolve("pi:openrouter/qwen/qwen3-coder:free")
        self.assertTrue(got.ready, got.reason)
        settings = got.agent_provider["settings"]
        assert isinstance(settings, dict)
        self.assertEqual(["qwen/qwen3-coder:free"], settings["models"])

    def test_pi_not_ready_says_why(self) -> None:
        self.pi_config({
            "local": {"baseUrl": "http://localhost:11434/v1", "apiKey": "X_KEY"},
            "odd": {"baseUrl": "https://a.example/v1", "api": "anthropic", "apiKey": "X_KEY"},
            "nokey": {"baseUrl": "https://a.example/v1"},
            "literal": {"baseUrl": "https://a.example/v1", "apiKey": "sk-123"}})
        for value, why in (("pi:nowhere/m", "pi has no provider nowhere"),
                           ("pi:local/m", "isn't a public HTTPS endpoint"),
                           ("pi:odd/m", "isn't OpenAI-compatible"),
                           ("pi:nokey/m", "has no apiKey"),
                           ("pi:groq/m", "no GROQ_API_KEY in the host secrets (add it to"),
                           ("pi:literal/m", "no PI_LITERAL_API_KEY in the host secrets "
                                            "(the key is written in")):
            with self.subTest(value=value):
                got = ap.resolve(value)
                self.assertFalse(got.ready)
                self.assertIn(why, got.reason)

    def test_the_pi_starters_own_model_is_still_read(self) -> None:
        # `read_local_model` (setup's pi starter) shares the provider lookup.
        from bot_bottle.contrib.pi import starter  # pylint: disable=import-outside-toplevel
        self.pi_config({"groq": {"baseUrl": "https://api.groq.com/openai/v1",
                                 "apiKey": "GROQ_API_KEY"}})
        (self.home / ".pi" / "agent" / "settings.json").write_text(json.dumps(
            {"defaultProvider": "groq", "defaultModel": "m"}))
        local = starter.read_local_model({"HOME": str(self.home)})
        assert local is not None
        self.assertEqual(("groq", "m", "GROQ_API_KEY"), (local.provider, local.model,
                                                          local.key_name))
        (self.home / ".pi" / "agent" / "settings.json").write_text(json.dumps(
            {"defaultProvider": "nowhere", "defaultModel": "m"}))
        self.assertIsNone(starter.read_local_model({"HOME": str(self.home)}))


class TheConsoleLink(_Store):
    def setUp(self) -> None:
        super().setUp()
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.control.hostctl import remote
        self.remote = remote
        self.enterContext(mock.patch.object(remote.host_stats, "snapshot",
                                            return_value={"architect": {}}))

    def connect(self) -> tuple[Any, list[dict[str, Any]]]:
        sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        return self.remote.ConsoleLink(mock.Mock(), send, agents=lambda: []), sent

    def test_the_architect_message_is_kept_and_reported_at_once(self) -> None:
        self.assertIn("architect", self.remote.ConsoleLink.handles())
        link, sent = self.connect()
        for _ in range(2):
            asyncio.run(link.handle({"type": "architect", "host": OTHER,
                                     "connection": "codex", "label": "delphi"}))
        self.assertEqual(ArchitectValues(OTHER, "codex", "delphi"), self.store.architect()[1])
        self.assertEqual(["host_stats"], [m["type"] for m in sent])  # once: one change

    def test_adoption_is_offered_once_per_connect(self) -> None:
        self.store.set_host_config(HostConfig(architect_connection="codex"),
                                   ["architect_connection"])
        offer = {"type": "architect.set", "host": ME, "connection": "codex", "adopt": True}
        for _connect in range(2):
            link, sent = self.connect()
            for _ in range(2):
                asyncio.run(link.handle({"type": "architect", "host": ""}))
            self.assertEqual([offer], [m for m in sent if m["type"] == "architect.set"])

    def test_no_adoption_when_the_account_has_one_or_this_host_is_off(self) -> None:
        link, sent = self.connect()
        asyncio.run(link.handle({"type": "architect", "host": OTHER}))
        asyncio.run(link.handle({"type": "architect", "host": "off"}))
        self.store.set_host_config(HostConfig(architect_host="off"), ["architect_host"])
        asyncio.run(link.handle({"type": "architect", "host": ""}))
        self.assertEqual([], [m for m in sent if m["type"] == "architect.set"])


class _Console:
    """A connected console on its own loop: it answers each `architect.set`
    with the account's values (`echo`), as the console does."""

    def __init__(self, store: ConfigStore, echo: bool = True) -> None:
        self.store, self.echo = store, echo
        self.sent: list[dict[str, Any]] = []
        self.account_when_sent: list[ArchitectValues] = []
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

        async def attach() -> object:
            return architect_setting.attach(self.send)
        self.token = asyncio.run_coroutine_threadsafe(attach(), self.loop).result(5)

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)
        self.account_when_sent.append(self.store.architect()[1])
        if self.echo:
            before = self.store.architect()[1]
            architect_setting.received(
                {"type": "architect", "host": message.get("host", before.host),
                 "connection": message.get("connection", before.connection)}, self.store)

    def close(self) -> None:
        architect_setting.detach(self.token)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()


class Changing(_Store):
    """`POST /architect` (`config set|unset`)."""

    def console(self, echo: bool = True) -> _Console:
        console = _Console(self.store, echo)
        self.addCleanup(console.close)
        return console

    def test_paired_set_asks_the_console_and_reports_its_answer(self) -> None:
        self.account("", "auto")
        console = self.console()
        reply = architect_setting.change({"host": OTHER})
        self.assertEqual([{"type": "architect.set", "host": OTHER}], console.sent)
        # The host didn't write the account's copy itself: the console's answer did.
        self.assertEqual("", console.account_when_sent[0].host)
        self.assertEqual((OTHER, False), (reply["host"], reply["is_architect"]))
        reply = architect_setting.change({"connection": "codex"})
        self.assertEqual({"type": "architect.set", "connection": "codex"}, console.sent[-1])
        self.assertEqual("codex", reply["connection"])

    def test_paired_local_is_this_hosts_id(self) -> None:
        console = self.console()
        reply = architect_setting.change({"host": "local", "connection": "claude"})
        self.assertEqual([{"type": "architect.set", "host": ME, "connection": "claude"}],
                         console.sent)
        self.assertTrue(reply["is_architect"])

    def test_paired_without_the_link_is_an_error(self) -> None:
        with self.assertRaisesRegex(architect_setting.Unreachable,
                                    "can't reach the console; the architect setting is the "
                                    "account's"):
            architect_setting.change({"host": "off"})

    def test_paired_without_an_answer_is_an_error(self) -> None:
        console = self.console(echo=False)
        with mock.patch.object(architect_setting, "ECHO_SECONDS", 0.2), \
             self.assertRaisesRegex(architect_setting.Unreachable, "didn't answer"):
            architect_setting.change({"host": "off"})
        self.assertEqual(1, len(console.sent))
        self.assertEqual("", self.store.architect()[1].host)

    def test_paired_unset_is_refused(self) -> None:
        for name in ("host", "connection"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "paired"):
                architect_setting.change({"unset": name})

    def test_bad_values_are_refused_before_asking(self) -> None:
        console = self.console()
        bodies: list[dict[str, object]] = [{"host": "a b"}, {"connection": "off"}, {},
                                           {"other": 1}, {"unset": "host", "host": "off"}]
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(ValueError):
                architect_setting.change(body)
        self.assertEqual([], console.sent)

    def test_unpaired_set_and_unset_are_this_hosts_own(self) -> None:
        self.own_id.return_value = None
        console = self.console()
        reply = architect_setting.change({"host": "off", "connection": "codex"})
        self.assertEqual(("off", "codex", False), (reply["host"], reply["connection"],
                                                   reply["is_architect"]))
        self.assertEqual(ArchitectValues("off", "codex"), self.store.architect()[0])
        architect_setting.change({"unset": "host"})
        self.assertEqual(ArchitectValues("local", "codex"), self.store.architect()[0])
        architect_setting.change({"unset": "connection"})
        self.assertEqual(ArchitectValues("local", "auto"), self.store.architect()[0])
        with self.assertRaisesRegex(ValueError, "local .this host. or off"):
            architect_setting.change({"host": OTHER})
        self.assertEqual([], console.sent)  # never the console


class Suggesting(unittest.TestCase):
    """The flow still answers everywhere: the architect where this host is
    it and it's ready, else the rules, saying why."""

    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = tmp / ".bot-bottle"
        (self.root / "bottles").mkdir(parents=True)

    def _suggest(self, provider: str, ready: tuple[str, ...] = ("codex",),
                 not_here: str = "") -> Any:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.suggest.flow import suggest
        asked: list[Any] = []

        def run(architect: Any, _prompt: str) -> str:
            asked.append(architect)
            return '{"option": "reuse", "bottles": ["basic"]}'

        with mock.patch("bot_bottle.agent_provider.harness_ready",
                        side_effect=_only(*ready)):
            proposal = suggest("deploy it", None, run=run, provider=provider,
                               host_root=self.root, not_here=not_here)
        return proposal, asked

    def test_a_ready_connection_answers(self) -> None:
        proposal, asked = self._suggest("codex")
        self.assertEqual("architect", proposal.source)
        self.assertEqual(("codex", "codex"), (asked[0].provider, asked[0].harness))
        proposal, asked = self._suggest("auto")
        self.assertEqual(("auto", "codex"), (asked[0].provider, asked[0].harness))

    def test_not_ready_is_the_rules_saying_why(self) -> None:
        proposal, asked = self._suggest("claude")
        self.assertEqual(("rules", []), (proposal.source, asked))
        self.assertTrue(any("claude isn't installed and logged in" in n for n in proposal.notes))

    def test_not_the_architect_host_is_the_rules_without_asking(self) -> None:
        proposal, asked = self._suggest("codex", not_here=architect_setting.NO_HOST)
        self.assertEqual(("rules", []), (proposal.source, asked))
        self.assertIn("No architect host: choose one in Settings, or `bot-bottle config set "
                      "bottle-architect.host <host>`; this is the rules path", proposal.notes)

    def test_the_host_controllers_routes_pass_the_setting(self) -> None:
        # pylint: disable=import-outside-toplevel
        from bot_bottle.control.hostctl import suggest_link, suggest_routes
        from bot_bottle.contract.hostctl import Settings
        found = architect_setting.InForce("off", "codex", False, "the bottle architect is off")
        with mock.patch.object(architect_setting, "in_force", return_value=found), \
             mock.patch("bot_bottle.suggest.flow.suggest") as flow:
            suggest_routes.suggest("t", None, False, Settings())
            suggest_link._suggest("t", None, [])  # pylint: disable=protected-access
        self.assertEqual([("codex", "the bottle architect is off")] * 2,
                         [(c.kwargs["provider"], c.kwargs["not_here"])
                          for c in flow.call_args_list])

    def test_the_warm_bottle_boots_only_on_the_architect_host(self) -> None:
        # pylint: disable=import-outside-toplevel,protected-access
        from bot_bottle.control.hostctl import architect_bottle
        from bot_bottle.suggest.architect import ArchitectError
        elsewhere = architect_setting.InForce(OTHER, "codex", False, "The architect host is x")
        with mock.patch.object(architect_setting, "in_force", return_value=elsewhere), \
             self.assertRaisesRegex(ArchitectError, "The architect host is x"):
            architect_bottle._configured()
        here = architect_setting.InForce(ME, "codex", True)
        with mock.patch.object(architect_setting, "in_force", return_value=here), \
             mock.patch("bot_bottle.agent_provider.harness_ready", side_effect=_only("codex")):
            self.assertEqual("codex", architect_bottle._configured().harness)


class OldSetupCopies(unittest.TestCase):
    """An `agents/bottle-architect.md` an earlier setup wrote: removed if
    unchanged, on any harness; a changed one stays as an ordinary agent."""

    def setUp(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home, self.root = tmp, tmp / ".bot-bottle"
        (self.root / "agents").mkdir(parents=True)
        self.enterContext(mock.patch.dict(os.environ, {"HOME": str(tmp),
                                                        "BOT_BOTTLE_ROOT": str(self.root)}))

    def written_by_setup(self, harness: str) -> Path:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.manifest.versions import record
        path = self.root / "agents" / f"{ARCHITECT_AGENT}.md"
        path.write_text(f"---\n# Written by bot-bottle setup.\nagent_provider:\n"
                        f"  harness: {harness}\n---\n\nYou are bottle-architect.\n")
        record(f"setup: write agents/{ARCHITECT_AGENT}", root=self.root)
        return path

    def test_it_is_one_of_the_agents_adopted(self) -> None:
        self.assertIn(ARCHITECT_AGENT, adoptable_agents())
        self.assertIn("claude", adoptable_agents())

    def test_an_unchanged_copy_goes_on_any_harness(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.manifest.versions import adopt_builtin_agents
        for harness in ("claude", "codex", "pi"):
            with self.subTest(harness=harness):
                path = self.written_by_setup(harness)
                self.assertEqual([ARCHITECT_AGENT],
                                 adopt_builtin_agents(adoptable_agents(), root=self.root))
                self.assertFalse(path.exists())

    def test_a_changed_copy_stays_listed_and_setup_and_doctor_note_it(self) -> None:
        # pylint: disable=import-outside-toplevel
        from bot_bottle.cli.commands import doctor
        from bot_bottle.cli.commands.setup import Setup
        from bot_bottle.manifest import ManifestIndex
        path = self.written_by_setup("pi")
        path.write_text(path.read_text() + "Be brief.\n")
        lines: list[str] = []
        Setup(home=self.home, host_env={"HOME": str(self.home), "PATH": ""},
              interactive=False, say=lines.append).run()
        self.assertTrue(path.exists())
        self.assertIn(f"note: {ARCHITECT_NOTE}", lines)
        self.assertIn("bottle-architect.host", ARCHITECT_NOTE)
        self.assertIn("bottle-architect.connection", ARCHITECT_NOTE)
        index = ManifestIndex.resolve(str(self.home))
        self.assertIn(ARCHITECT_AGENT, index.all_agent_names)
        self.assertEqual("pi", index.load_for_agent(ARCHITECT_AGENT).agent.agent_provider.harness)
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(doctor, "get_provider") as provider:
            provider.return_value.detect_host.return_value = mock.Mock(installed=False)
            doctor._check_providers()  # pylint: disable=protected-access
        self.assertIn(f"note: {ARCHITECT_AGENT}: {ARCHITECT_NOTE}", out.getvalue())


class TheCli(unittest.TestCase):
    def run_config(self, *argv: str, found: dict[str, Any] | None = None,
                   error: Exception | None = None) -> tuple[int, str, str]:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.commands import config as config_cmd
        found = found if found is not None else {"host": "local", "connection": "auto"}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(config_cmd.host, "architect", return_value=found,
                               side_effect=error), \
             mock.patch.object(config_cmd.host, "set_architect", return_value=found,
                               side_effect=error) as setter, \
             redirect_stdout(out), redirect_stderr(err):
            code = config_cmd.cmd_config(list(argv))
        self.setter = setter  # pylint: disable=attribute-defined-outside-init
        return code, out.getvalue(), err.getvalue()

    def test_set_unset_get_and_list(self) -> None:
        code, out, _ = self.run_config("set", "bottle-architect.host", "off",
                                       found={"host": "off", "connection": "auto"})
        self.assertEqual((0, "bottle-architect.host = off\n"), (code, out))
        self.assertEqual({"host": "off"}, self.setter.call_args.args[0])
        code, out, _ = self.run_config("set", "bottle-architect.connection", "codex",
                                       found={"host": ME, "connection": "codex"})
        self.assertEqual((0, "bottle-architect.connection = codex\n"), (code, out))
        self.assertEqual({"connection": "codex"}, self.setter.call_args.args[0])
        code, out, _ = self.run_config("unset", "bottle-architect.host")
        self.assertEqual((0, "bottle-architect.host = local\n"), (code, out))
        self.assertEqual({"unset": "host"}, self.setter.call_args.args[0])
        found = {"host": "", "connection": "claude"}
        self.assertEqual((0, "(none)\n", ""), self.run_config(
            "get", "bottle-architect.host", found=found))
        self.assertEqual((0, "bottle-architect.connection = claude\n"
                             "bottle-architect.host = (none)\n", ""),
                         self.run_config(found=found))

    def test_a_bad_connection_is_refused_before_asking(self) -> None:
        code, _, err = self.run_config("set", "bottle-architect.connection", "gpt-5")
        self.assertEqual(2, code)
        self.assertIn("auto, claude, codex or pi:<provider>/<model>", err)
        self.setter.assert_not_called()

    def test_the_host_controllers_refusal_is_shown(self) -> None:
        # pylint: disable=import-outside-toplevel
        from bot_bottle.control.hostctl.client import HostControlRefused
        from bot_bottle.control.hostctl.runner import HostControlError
        unreachable = json.dumps({"error": architect_setting.UNREACHABLE})
        for error, shown in ((HostControlRefused(409, "/architect", unreachable),
                              "can't reach the console"),
                             (HostControlError("down"), "down")):
            with self.subTest(shown=shown):
                code, _, err = self.run_config("set", "bottle-architect.host", "off",
                                               error=error)
                self.assertEqual(1, code)
                self.assertIn(shown, err)

    def test_the_status_line(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli.commands.lifecycle import architect_line
        cases = (({"host": ME, "is_architect": True, "ready": True,
                   "label": "this host, auto → claude"}, "architect: this host, auto → claude"),
                 ({"host": ME, "is_architect": True, "ready": False, "label": "this host, claude",
                   "reason": "claude isn't installed"},
                  "architect: this host, claude; not ready, so suggestions use the rules: "
                  "claude isn't installed"),
                 ({"host": OTHER, "is_architect": False, "ready": False, "label": "delphi",
                   "reason": "The architect host is delphi"}, "architect: delphi"),
                 ({"host": "off", "is_architect": False, "ready": False, "label": "off",
                   "reason": "off"}, "architect: off"),
                 ({"host": "", "is_architect": False, "ready": False, "label": "none",
                   "reason": architect_setting.NO_HOST},
                  f"architect: none; {architect_setting.NO_HOST}"),
                 (None, "architect: unknown (the host controller isn't running)"))
        for reply, line in cases:
            with self.subTest(line=line), \
                 mock.patch("bot_bottle.cli.host_state_client.running_architect",
                            return_value=reply):
                self.assertEqual(line, architect_line())

    def test_the_status_line_when_the_host_controller_fails(self) -> None:
        # pylint: disable=import-outside-toplevel
        from bot_bottle.cli.commands.lifecycle import architect_line
        from bot_bottle.control.hostctl.runner import HostControlError
        with mock.patch("bot_bottle.cli.host_state_client.running_architect",
                        side_effect=HostControlError("refused")):
            self.assertEqual("architect: unknown (refused)", architect_line())

    def test_running_architect_asks_a_running_host_controller_only(self) -> None:
        # pylint: disable-next=import-outside-toplevel
        from bot_bottle.cli import host_state_client
        with mock.patch.object(host_state_client.runner, "running", return_value=None):
            self.assertIsNone(host_state_client.running_architect())
        with mock.patch.object(host_state_client.runner, "running", return_value=mock.Mock()), \
             mock.patch.object(host_state_client.runner, "cli_token", return_value="t"), \
             mock.patch.object(host_state_client, "HostControlClient") as client:
            client.return_value.get.return_value = {"host": "local"}
            self.assertEqual({"host": "local"}, host_state_client.running_architect())
        self.assertEqual("/architect", client.return_value.get.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
