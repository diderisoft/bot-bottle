"""Unit: answering an agent's requests from the terminal (PRD 0094 chunk 4)."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from bot_bottle.approvals import Request, save_to_project
from bot_bottle.cli.commands.approve import Reviewer, describe
from bot_bottle.contract.yaml_subset import parse_frontmatter
from bot_bottle.control.client import AlreadyAnswered, OrchestratorClientError
from bot_bottle.control.hostctl.requests import RequestNotifier, summaries
from bot_bottle.manifest import ManifestIndex, trust

_CURRENT = ('routes:\n  - host: "github.com"\n    inspect:\n      matches:\n'
            '        - methods: ["GET"]\n')
_STRIPE = ('  - host: "api.stripe.com"\n    inspect:\n      matches:\n'
           '        - paths:\n            - type: "prefix"\n              value: "/v1/"\n'
           '          methods: ["GET", "POST"]\n')
_TOKEN = ('  - host: "api.example.com"\n    inspect:\n      auth_scheme: "Bearer"\n'
          '      token_env: "EXAMPLE_TOKEN"\n')


def _pending(proposed: str, *, pid: str = "p1", label: str = "pi",
             tool: str = "egress-allow") -> dict[str, Any]:
    return {"id": pid, "bottle_slug": "b-123", "bottle_label": label, "tool": tool,
            "justification": "fetch the API schema", "proposed_file": proposed,
            "current_file": _CURRENT}


class Requests(unittest.TestCase):
    def test_a_new_route_is_summarised_by_host_and_methods(self) -> None:
        request = Request.from_dict(_pending(_CURRENT + _STRIPE))
        self.assertEqual("pi wants GET, POST api.stripe.com", request.summary())
        self.assertEqual(["api.stripe.com"], [r.host for r in request.new_routes()])
        self.assertTrue(request.can_save_to_project())

    def test_a_route_with_a_credential_is_allow_once_only(self) -> None:
        request = Request.from_dict(_pending(_CURRENT + _TOKEN))
        self.assertFalse(request.can_save_to_project())
        self.assertIn("with $EXAMPLE_TOKEN", "\n".join(describe(request)))

    def test_the_popup_aligns_hosts_and_colors_the_credential(self) -> None:
        from bot_bottle.approvals import route_lines

        request = Request.from_dict(_pending(_CURRENT + _TOKEN))
        shown = "\n".join(describe(request))
        self.assertIn("api.example.com" + " " * 17 + "  ", shown)  # padded to 32
        self.assertIn("\x1b[38;5;173mwith $EXAMPLE_TOKEN\x1b[0m", shown)
        # The phone gets the same line in plain text.
        self.assertEqual(["api.example.com  any  with $EXAMPLE_TOKEN"], route_lines(request))

    def test_other_tools_get_a_plain_summary(self) -> None:
        request = Request.from_dict(_pending("an allowlist line", tool="gitleaks-allow"))
        self.assertEqual("pi wants to push past a secret-scan finding", request.summary())
        self.assertEqual([], request.new_routes())

    def test_unparseable_proposals_add_nothing(self) -> None:
        self.assertEqual([], Request.from_dict(_pending("routes: [nonsense")).new_routes())


class SaveToProject(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.home, self.project = base / "home", base / "app"
        (self.home / ".bot-bottle" / "agents").mkdir(parents=True)
        self.project.mkdir()
        env = patch.dict(os.environ, {"HOME": str(self.home),
                                      "BOT_BOTTLE_ROOT": str(self.home / ".bot-bottle")})
        env.start()
        self.addCleanup(env.stop)

    def test_creates_a_trusted_bottle_extending_basic(self) -> None:
        routes = Request.from_dict(_pending(_CURRENT + _STRIPE)).new_routes()
        path = save_to_project(self.project, routes)
        self.assertEqual(self.project / ".bot-bottle" / "bottles" / "main.md", path)
        frontmatter, _ = parse_frontmatter(path.read_text())
        self.assertEqual("basic", frontmatter["extends"])
        # Trusted already: it loads without asking.
        index = ManifestIndex.resolve(str(self.project))
        from bot_bottle.manifest.loader import load_bottle_chain  # pylint: disable=import-outside-toplevel
        bottle, sources = load_bottle_chain("./main", index.bottle_dirs())
        trust.check("./main", bottle, sources)
        route = next(r for r in bottle.egress.routes if r.Host == "api.stripe.com")
        self.assertEqual(("GET", "POST"), route.Matches[0].Methods)
        self.assertEqual("/v1/", route.Matches[0].Paths[0].Value)

    def test_adds_to_an_existing_bottle_and_replaces_the_same_host(self) -> None:
        routes = Request.from_dict(_pending(_CURRENT + _STRIPE)).new_routes()
        save_to_project(self.project, routes)
        path = save_to_project(self.project, routes)
        frontmatter, _ = parse_frontmatter(path.read_text())
        hosts = [r["host"] for r in frontmatter["egress"]["routes"]]  # type: ignore[index]
        self.assertEqual(["api.stripe.com"], hosts)


class Notifier(unittest.TestCase):
    def test_each_session_shows_its_oldest_request_and_a_count(self) -> None:
        pending = [_pending(_CURRENT + _STRIPE), _pending(_CURRENT + _TOKEN, pid="p2"),
                   _pending(_CURRENT + _STRIPE, label="other")]
        self.assertEqual({"pi": "pi wants GET, POST api.stripe.com (+1 more)", "idle": ""},
                         summaries(pending, ["pi", "idle"]))

    def test_only_changes_are_written_and_a_failure_backs_off(self) -> None:
        written: list[tuple[str, str]] = []
        replies: list[Any] = [[_pending(_CURRENT + _STRIPE)], [_pending(_CURRENT + _STRIPE)], [],
                              OSError("orchestrator down")]

        def fetch() -> list[dict[str, object]]:
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        notifier = RequestNotifier(lambda: ["pi"], fetch,
                                   lambda s, t: written.append((s, t)))
        self.assertEqual([True, True, True, False], [notifier.tick() for _ in range(4)])
        self.assertEqual([("pi", "pi wants GET, POST api.stripe.com"), ("pi", "")], written)


class Review(unittest.TestCase):
    def reviewer(self, keys: str, pending: list[dict[str, Any]]) -> tuple[Reviewer, MagicMock]:
        client = MagicMock()
        queue = list(pending)

        def respond(pid: str, **_kwargs: object) -> None:
            queue[:] = [p for p in queue if p["id"] != pid]

        client.supervise_pending.side_effect = lambda: list(queue)
        client.supervise_respond.side_effect = respond
        it = iter(keys)
        return Reviewer(client.supervise_pending, lambda _url: client, "pi",
                        key=lambda: next(it), out=lambda _s: None), client

    def test_allow_once_then_deny(self) -> None:
        reviewer, client = self.reviewer("ad", [_pending(_CURRENT + _STRIPE),
                                                _pending(_CURRENT + _TOKEN, pid="p2")])
        with patch("bot_bottle.cli.commands.approve.session_project", return_value=None):
            self.assertEqual(0, reviewer.run())
        decisions = [c.kwargs["decision"] for c in client.supervise_respond.call_args_list]
        self.assertEqual(["approve", "reject"], decisions)
        self.assertEqual("b-123", client.supervise_respond.call_args.kwargs["bottle_slug"])

    def test_p_is_ignored_without_a_project_and_skip_leaves_it_pending(self) -> None:
        reviewer, client = self.reviewer("ps", [_pending(_CURRENT + _STRIPE)])
        with patch("bot_bottle.cli.commands.approve.session_project", return_value=None):
            reviewer.run()
        client.supervise_respond.assert_not_called()

    def test_allow_for_project_answers_and_saves(self) -> None:
        reviewer, client = self.reviewer("p", [_pending(_CURRENT + _STRIPE)])
        shown: list[str] = []
        reviewer.out = shown.append
        with patch("bot_bottle.cli.commands.approve.session_project",
                   return_value=Path("/proj")), \
                patch("bot_bottle.approvals.session_project", return_value=Path("/proj")), \
                patch("bot_bottle.approvals.save_to_project",
                      return_value=Path("/proj/.bot-bottle/bottles/proj.md")) as save, \
                patch("sys.stdout", io.StringIO()):
            reviewer.run()
        self.assertEqual("approve", client.supervise_respond.call_args.kwargs["decision"])
        self.assertEqual(Path("/proj"), save.call_args.args[0])
        self.assertIn("allowed, and saved to ./.bot-bottle/bottles/proj.md in the project", shown)
        self.assertEqual(["api.stripe.com"], [r.host for r in save.call_args.args[1]])

    def test_an_answer_from_the_phone_first_is_shown(self) -> None:
        reviewer, client = self.reviewer("ax", [_pending(_CURRENT + _STRIPE)])
        shown: list[str] = []
        reviewer.out = shown.append

        def answered_elsewhere(pid: str, **_kwargs: object) -> None:
            raise AlreadyAnswered("rejected")

        client.supervise_respond.side_effect = answered_elsewhere
        # Then it's gone from the queue.
        client.supervise_pending.side_effect = [[_pending(_CURRENT + _STRIPE)], []]
        with patch("bot_bottle.cli.commands.approve.session_project", return_value=None):
            self.assertEqual(0, reviewer.run())
        self.assertIn("already answered elsewhere: rejected; yours wasn't applied "
                      "(press a key)", shown)

    def test_each_answer_goes_to_the_orchestrator_holding_it(self) -> None:
        docker, firecracker = MagicMock(), MagicMock()
        clients = {"http://docker": docker, "http://fc": firecracker}
        queue = [{**_pending(_CURRENT + _STRIPE), "orchestrator_url": "http://fc"}]

        def respond(pid: str, **_kwargs: object) -> None:
            queue[:] = [p for p in queue if p["id"] != pid]

        firecracker.supervise_respond.side_effect = respond
        it = iter("a")
        reviewer = Reviewer(lambda: list(queue), clients.__getitem__, "pi",
                            key=lambda: next(it), out=lambda _s: None)
        with patch("bot_bottle.cli.commands.approve.session_project", return_value=None):
            self.assertEqual(0, reviewer.run())
        firecracker.supervise_respond.assert_called_once()
        docker.supervise_respond.assert_not_called()

    def test_the_command_lists_every_orchestrator_and_reports_none(self) -> None:
        from bot_bottle.cli.commands import approve  # pylint: disable=import-outside-toplevel

        with patch.object(approve, "pending_everywhere", return_value=[]) as listed, \
                patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(0, approve.cmd_approve(["pi"]))
        listed.assert_called_once()
        self.assertIn("nothing waiting", out.getvalue())
        with patch.object(approve, "pending_everywhere",
                          side_effect=OrchestratorClientError("none running")), \
                patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(1, approve.cmd_approve([]))
        self.assertIn("couldn't reach the orchestrator: none running", out.getvalue())

    def test_another_sessions_requests_are_not_shown(self) -> None:
        reviewer, client = self.reviewer("", [_pending(_CURRENT + _STRIPE, label="other")])
        self.assertEqual(0, reviewer.run())
        client.supervise_respond.assert_not_called()


if __name__ == "__main__":
    unittest.main()
