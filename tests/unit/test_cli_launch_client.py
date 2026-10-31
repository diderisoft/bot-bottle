"""Unit: the CLI as a host controller client (PRD 0092 chunk 5): the secrets a
launch sends, the confirm-and-resend loop, attaching and the exit code, and
`active` / `cleanup` over the daemon's bottles. The daemon client is mocked."""

from __future__ import annotations

import contextlib
import io
import unittest
from typing import Any
from unittest import mock

from bot_bottle.backend import ActiveAgent
from bot_bottle.cli import launch_client
from bot_bottle.cli.commands import active as active_cmd
from bot_bottle.cli.commands import cleanup as cleanup_cmd
from bot_bottle.contract.hostctl import BOTTLES_PATH, CONFIRM_ERROR, LaunchRequest
from bot_bottle.lib.log import Die
from bot_bottle.manifest import ManifestIndex
from bot_bottle.secret_source import MappingSecrets


def _index(env: dict[str, str], *, egress: bool = False, forge: bool = False) -> ManifestIndex:
    bottle: dict[str, Any] = {"env": env}
    if egress:
        bottle["egress"] = {"routes": [{
            "host": "api.example", "inspect": {"auth": {"scheme": "Bearer", "token_ref": "TOK"}},
        }]}
    if forge:
        bottle["credentials"] = {"forge-api": {"source": "secret", "secret": "FORGE"}}
        bottle["git-gate"] = {
            "forges": {"example.com": {"kind": "gitea", "url": "https://example.com",
                                       "token": "$forge-api", "host_key": "ssh-ed25519 AAAA"}},
            "repos": ["example.com/x/y"]}
    return ManifestIndex.from_json_obj({
        "bottles": {"dev": bottle},
        "agents": {"demo": {"skills": [], "prompt": ""}},
    })


class TestSecrets(unittest.TestCase):
    def test_only_the_names_the_bottle_needs_are_sent(self) -> None:
        manifest = _index({"A": "${HOST_A}", "B": "?Your B", "C": "literal"},
                          egress=True, forge=True).load_for_agent("demo", ("dev",))
        self.assertEqual({"HOST_A": "", "B": "?Your B", "TOK": "", "FORGE": ""},
                         launch_client.needed_secrets(manifest))
        source = MappingSecrets({"HOST_A": "a", "B": "b", "TOK": "t", "FORGE": "f",
                                 "UNRELATED": "nope"})
        self.assertEqual({"HOST_A": "a", "B": "b", "TOK": "t", "FORGE": "f"},
                         launch_client.resolve_secrets(manifest, source, prompt=False))

    def test_tmux_attach_env_drops_tmux_and_needs_a_usable_term(self) -> None:
        with mock.patch.object(launch_client.shutil, "which", return_value="/usr/bin/tmux"):
            for term, expected in ((None, "xterm-256color"), ("dumb", "xterm-256color"),
                                   ("screen-256color", "screen-256color")):
                env = {"TMUX": "/tmp/x", **({"TERM": term} if term else {})}
                with mock.patch.dict(launch_client.os.environ, env, clear=True):
                    argv, out = launch_client.tmux_attach("s1")
                self.assertEqual(expected, out["TERM"])
                self.assertNotIn("TMUX", out)
                self.assertEqual("/usr/bin/tmux", argv[0])

    def test_the_provider_token_names_are_sent_too(self) -> None:
        for provider, name in (
            ({"harness": "claude", "auth_token": "CLAUDE_TOK"}, "CLAUDE_TOK"),
            ({"harness": "pi", "settings": {"api_key_env": "PI_KEY"}}, "PI_KEY"),
        ):
            index = ManifestIndex.from_json_obj({
                "bottles": {"dev": {"env": {}}},
                "agents": {"demo": {"skills": [], "prompt": "",
                                    "agent_provider": provider}},
            })
            manifest = index.load_for_agent("demo", ("dev",))
            self.assertIn(name, launch_client.needed_secrets(manifest))

    def test_missing_question_entries_are_prompted_only_when_interactive(self) -> None:
        manifest = _index({"B": "?Your B", "A": "${HOST_A}"}).load_for_agent("demo", ("dev",))
        with mock.patch.object(launch_client, "prompt_secret", return_value="typed") as ask:
            self.assertEqual({"B": "typed"}, launch_client.resolve_secrets(
                manifest, MappingSecrets({}), prompt=True))
            ask.assert_called_once_with("B", "?Your B")
            ask.reset_mock()
            self.assertEqual({}, launch_client.resolve_secrets(
                manifest, MappingSecrets({}), prompt=False))
            ask.assert_not_called()

    def test_prepare_request_fills_secrets_and_skips_host_keys_headless(self) -> None:
        index = _index({"A": "${HOST_A}"})
        with mock.patch.object(launch_client, "secrets_from_env",
                               return_value=MappingSecrets({"HOST_A": "a"})), \
             mock.patch.object(launch_client, "confirm_host_keys") as keys:
            request = launch_client.prepare_request(
                LaunchRequest(agent="demo", bottles=("dev",)), index, interactive=False)
        self.assertEqual({"HOST_A": "a"}, request.secrets)
        keys.assert_not_called()


def _client(finals: list[Any], states: list[Any]) -> mock.MagicMock:
    client = mock.MagicMock()
    client.post.side_effect = [{"job": f"j{i}"} for i in range(len(finals))]
    client.follow.side_effect = finals
    client.get.side_effect = states
    return client


class TestLaunch(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(mock.patch.object(launch_client.runner, "ensure_running"))
        self.enterContext(mock.patch.object(launch_client.runner, "cli_token", return_value="t"))
        self.run_attach = self.enterContext(mock.patch.object(launch_client.subprocess, "run"))
        self.enterContext(mock.patch.object(launch_client, "tmux_attach",
                                            return_value=(["tmux", "attach"], {})))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.ready = {"type": "result", "value": {"bottle": "b1", "session": "b1"}}

    def launch(self, client: mock.MagicMock, request: LaunchRequest | None = None) -> int:
        with mock.patch.object(launch_client, "HostControlClient", return_value=client):
            return launch_client.launch(request or LaunchRequest(agent="demo"))

    def test_attaches_and_returns_the_agents_exit_code(self) -> None:
        client = _client([self.ready], [{"running": False, "exit": {"status": 3, "signal": ""}}])
        self.assertEqual(3, self.launch(client))
        self.assertEqual(BOTTLES_PATH, client.post.call_args.args[0])
        self.run_attach.assert_called_once()
        client.get.assert_called_once_with("/sessions/b1")

    def test_a_signal_exit_maps_to_128_plus(self) -> None:
        client = _client([self.ready], [{"running": False,
                                         "exit": {"status": 0, "signal": "SIGTERM"}}])
        self.assertEqual(143, self.launch(client))

    def test_detaching_leaves_the_agent_running(self) -> None:
        client = _client([self.ready], [{"running": True, "exit": None}])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(0, self.launch(client))
        self.assertIn("attach b1", err.getvalue())

    def test_waits_briefly_for_the_exit_report(self) -> None:
        client = _client([self.ready], [{"running": False, "exit": None},
                                        {"running": False, "exit": {"status": 0, "signal": ""}}])
        self.assertEqual(0, self.launch(client))

    def test_confirmation_resends_with_assume_yes(self) -> None:
        confirm = {"type": "error", "code": CONFIRM_ERROR, "message": "stale image\nLaunch anyway?"}
        client = _client([confirm, self.ready], [{"running": False, "exit": {"status": 0}}])
        with mock.patch.object(launch_client, "read_tty_line", return_value="y"):
            self.assertEqual(0, self.launch(client))
        first, second = (c.args[1] for c in client.post.call_args_list)
        self.assertEqual((False, True), (first["assume_yes"], second["assume_yes"]))

    def test_declining_stops_without_resending(self) -> None:
        confirm = {"type": "error", "code": CONFIRM_ERROR, "message": "stale image\nLaunch anyway?"}
        client = _client([confirm], [])
        with mock.patch.object(launch_client, "read_tty_line", return_value="n"):
            self.assertEqual(0, self.launch(client))
        self.assertEqual(1, client.post.call_count)
        self.run_attach.assert_not_called()

    def test_errors_die_and_assume_yes_never_prompts(self) -> None:
        confirm = {"type": "error", "code": CONFIRM_ERROR, "message": "stale"}
        with self.assertRaises(Die):
            self.launch(_client([confirm], []), LaunchRequest(agent="demo", assume_yes=True))
        with self.assertRaises(Die):
            self.launch(_client([{"type": "error", "message": "boom"}], []))
        with self.assertRaises(Die):  # Ctrl-C while the launch streams
            self.launch(_client([None], []))


class TestControllerRestartPrompt(unittest.TestCase):
    """#679: an updated controller holding bottles is restarted only on a yes."""

    def setUp(self) -> None:
        self.err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def test_without_a_terminal_the_answer_is_no(self) -> None:
        with mock.patch.object(launch_client.sys.stdin, "isatty", return_value=False), \
             mock.patch.object(launch_client, "read_tty_line") as read:
            self.assertFalse(launch_client.confirm_controller_restart(["b1"]))
        read.assert_not_called()

    def test_the_prompt_names_the_bottles_and_says_they_keep_running(self) -> None:
        for reply, expected in (("y", True), ("", False)):
            with self.subTest(reply=reply), \
                 mock.patch.object(launch_client.sys.stdin, "isatty", return_value=True), \
                 mock.patch.object(launch_client, "read_tty_line", return_value=reply):
                self.assertEqual(expected, launch_client.confirm_controller_restart(["b1", "b2"]))
        self.assertIn("moves 2 running bottle(s) (b1, b2)", self.err.getvalue())
        self.assertIn("agents keep running", self.err.getvalue())

    def test_a_headless_launch_never_restarts_the_controller(self) -> None:
        with mock.patch.object(launch_client.runner, "ensure_running",
                               side_effect=launch_client.runner.StaleHostControllerError("x")) as ensure, \
             mock.patch.object(launch_client.runner, "cli_token", return_value="t"), \
             self.assertRaises(Die):
            launch_client.launch(LaunchRequest(agent="demo", headless=True))
        confirm = ensure.call_args.kwargs["confirm_restart"]
        self.assertFalse(confirm(["b1"]))

    def test_an_interactive_launch_asks(self) -> None:
        with mock.patch.object(launch_client.runner, "ensure_running",
                               side_effect=launch_client.runner.StaleHostControllerError("x")) as ensure, \
             mock.patch.object(launch_client.runner, "cli_token", return_value="t"), \
             self.assertRaises(Die):
            launch_client.launch(LaunchRequest(agent="demo"))
        self.assertIs(launch_client.confirm_controller_restart,
                      ensure.call_args.kwargs["confirm_restart"])


class TestActive(unittest.TestCase):
    def _run(self, held: dict[str, Any], active: list[ActiveAgent]) -> str:
        out = io.StringIO()
        with mock.patch.object(active_cmd, "held_bottles", return_value=held), \
             mock.patch.object(active_cmd, "enumerate_active_agents", return_value=active), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(0, active_cmd.cmd_active([]))
        return out.getvalue()

    def test_shows_session_state_and_bottles_the_daemon_does_not_hold(self) -> None:
        held = {
            "b1": {"id": "b1", "backend": "docker", "agent": "demo", "label": "",
                   "session": {"running": True}},
            "b2": {"id": "b2", "backend": "docker", "agent": "demo", "label": "L",
                   "session": {"running": False}},
        }
        active = [
            ActiveAgent("docker", "b1", "demo", "", ("egress",)),
            ActiveAgent("docker", "other", "demo", "", ()),
        ]
        lines = self._run(held, active).splitlines()
        self.assertEqual(["docker\tb1\tdemo\tegress\trunning", "docker\tother\tdemo\t-\t-",
                          "docker\tb2\tL (demo)\t-\texited"], lines)

    def test_held_bottles_is_empty_without_a_daemon(self) -> None:
        with mock.patch.object(active_cmd.runner, "running", return_value=None):
            self.assertEqual({}, active_cmd.held_bottles())


class TestCleanup(unittest.TestCase):
    def test_tears_down_held_bottles_before_backend_cleanup(self) -> None:
        client = mock.MagicMock()
        client.delete.return_value = {"job": "j1"}
        client.follow.return_value = {"type": "result", "value": {}}
        with mock.patch.object(cleanup_cmd, "known_backend_names", return_value=[]), \
             mock.patch.object(cleanup_cmd, "held_bottles", return_value={"b1": {}}), \
             mock.patch.object(cleanup_cmd, "_prompt_yes", return_value=True), \
             mock.patch.object(cleanup_cmd.runner, "running"), \
             mock.patch.object(cleanup_cmd.runner, "cli_token", return_value="t"), \
             mock.patch.object(cleanup_cmd, "HostControlClient", return_value=client), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(0, cleanup_cmd.cmd_cleanup(["bots"]))
        client.delete.assert_called_once_with(f"{BOTTLES_PATH}/b1")

    def test_declining_tears_nothing_down(self) -> None:
        with mock.patch.object(cleanup_cmd, "known_backend_names", return_value=[]), \
             mock.patch.object(cleanup_cmd, "held_bottles", return_value={"b1": {}}), \
             mock.patch.object(cleanup_cmd, "_prompt_yes", return_value=False), \
             mock.patch.object(cleanup_cmd, "_teardown_held") as teardown, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(0, cleanup_cmd.cmd_cleanup(["bots"]))
        teardown.assert_not_called()


if __name__ == "__main__":
    unittest.main()
