"""Unit: supervise headless paths — the discovery + approve/reject that the
TUI key handlers call into.

These go through the orchestrator HTTP client now (the operator never
touches the DB directly), so the client is mocked here; the server-side
apply / response / audit is covered in test_orchestrator_service.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from bot_bottle.cli.commands import supervise as supervise_cli
from bot_bottle.orchestrator.supervisor import (
    Proposal,
    TOOL_EGRESS_ALLOW,
    TOOL_EGRESS_BLOCK,
    TOOL_GITLEAKS_ALLOW,
    TOOL_EGRESS_TOKEN_ALLOW,
)


FIXED = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _proposal(slug: str = "dev", tool: str = TOOL_EGRESS_ALLOW,
              *, now: datetime = FIXED) -> Proposal:
    payloads = {
        TOOL_EGRESS_ALLOW: "routes:\n  - host: example.com\n",
        TOOL_EGRESS_BLOCK: "routes:\n  - host: example.com\n",
        TOOL_GITLEAKS_ALLOW: "file: tests/test_fixture.py\nline: 3\n",
        TOOL_EGRESS_TOKEN_ALLOW: "host: api.example.com\ndetector: token\n",
    }
    payload = payloads.get(tool, "")
    return Proposal.new(
        bottle_slug=slug, tool=tool, proposed_file=payload,
        justification=f"needed for {slug}",
        now=now,
    )


class _ClientMixin:
    """Install a mock orchestrator client as the CLI-session singleton."""

    def _install_client(self, pending: "list[Proposal] | None" = None) -> MagicMock:
        client = MagicMock()
        client.supervise_pending.return_value = [
            p.to_dict() for p in (pending or [])
        ]
        patcher = patch.object(supervise_cli, "_client", return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)  # type: ignore[attr-defined]
        self.addCleanup(  # type: ignore[attr-defined]
            lambda: setattr(supervise_cli, "_client_instance", None))
        return client


class TestDiscoverPending(_ClientMixin, unittest.TestCase):
    def test_empty(self) -> None:
        self._install_client([])
        self.assertEqual([], supervise_cli.discover_pending())

    def test_lists_all_bottles(self) -> None:
        self._install_client([_proposal("dev"), _proposal("api")])
        pending = supervise_cli.discover_pending()
        self.assertEqual(
            {"dev", "api"}, {qp.proposal.bottle_slug for qp in pending})

    def test_sorted_by_arrival(self) -> None:
        early = _proposal(
            "api", now=datetime(2026, 5, 25, 10, 0, 0, tzinfo=timezone.utc))
        late = _proposal(
            "dev", now=datetime(2026, 5, 25, 14, 0, 0, tzinfo=timezone.utc))
        self._install_client([late, early])
        pending = supervise_cli.discover_pending()
        self.assertEqual([early.id, late.id], [qp.proposal.id for qp in pending])

    def test_label_comes_from_bottle_label(self) -> None:
        # The server tags each dict with the human slug; the CLI displays it
        # while the proposal stays keyed by the opaque bottle_id.
        client = MagicMock()
        d = _proposal("3601cbe883c2786d").to_dict()
        d["bottle_label"] = "codex-dev-a1b2c"
        client.supervise_pending.return_value = [d]
        with patch.object(supervise_cli, "_client", return_value=client):
            pending = supervise_cli.discover_pending()
        self.assertEqual("codex-dev-a1b2c", pending[0].label)
        self.assertEqual("3601cbe883c2786d", pending[0].proposal.bottle_slug)

    def test_label_falls_back_to_slug_when_absent(self) -> None:
        # Legacy dicts without bottle_label (e.g. an older orchestrator).
        self._install_client([_proposal("dev")])
        self.assertEqual("dev", supervise_cli.discover_pending()[0].label)


class TestApproveReject(_ClientMixin, unittest.TestCase):
    def _qp(self, tool: str = TOOL_EGRESS_ALLOW) -> "supervise_cli.QueuedProposal":
        return supervise_cli.QueuedProposal(proposal=_proposal(tool=tool))

    def test_approve_calls_respond(self) -> None:
        client = self._install_client()
        qp = self._qp()
        supervise_cli.approve(qp)
        client.supervise_respond.assert_called_once_with(
            qp.proposal.id, bottle_slug="dev", decision="approve",
            notes="", final_file=None,
        )

    def test_modify_sets_decision_and_final_file(self) -> None:
        client = self._install_client()
        qp = self._qp()
        edited = "routes:\n  - host: edited.example.com\n"
        supervise_cli.approve(qp, final_file=edited, notes="tweaked")
        client.supervise_respond.assert_called_once_with(
            qp.proposal.id, bottle_slug="dev", decision="modify",
            notes="tweaked", final_file=edited,
        )

    def test_reject_calls_respond(self) -> None:
        client = self._install_client()
        qp = self._qp()
        supervise_cli.reject(qp, reason="nope")
        client.supervise_respond.assert_called_once_with(
            qp.proposal.id, bottle_slug="dev", decision="reject", notes="nope",
        )

    def test_tui_report_only_requires_reason(self) -> None:
        self._install_client()
        qp = self._qp(tool=TOOL_GITLEAKS_ALLOW)
        with patch.object(supervise_cli, "_prompt", return_value=""):
            status = supervise_cli._approve_from_tui(None, qp)  # type: ignore[arg-type]
        self.assertEqual("approve aborted (empty reason)", status)

    def test_tui_report_only_writes_reason(self) -> None:
        client = self._install_client()
        qp = self._qp(tool=TOOL_GITLEAKS_ALLOW)
        with patch.object(supervise_cli, "_prompt", return_value="test fixture"):
            status = supervise_cli._approve_from_tui(None, qp)  # type: ignore[arg-type]
        self.assertIn("approved gitleaks-allow", status)
        client.supervise_respond.assert_called_once()
        self.assertEqual(
            "test fixture", client.supervise_respond.call_args.kwargs["notes"])

    def test_suffix_for_token_allow_is_txt(self) -> None:
        self.assertEqual(
            ".txt", supervise_cli._suffix_for_tool(TOOL_EGRESS_TOKEN_ALLOW))


class TestEditInEditor(unittest.TestCase):
    def test_runs_editor_returns_edited_content(self) -> None:
        original_editor = os.environ.get("EDITOR")
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".sh", delete=False, prefix="fake-editor.",
            ) as script:
                script.write('#!/bin/sh\nprintf "%s" "edited" > "$1"\n')
                editor_script = script.name
            os.chmod(editor_script, 0o755)
            os.environ["EDITOR"] = editor_script
            try:
                result = supervise_cli.edit_in_editor("original")
                self.assertEqual("edited", result)
            finally:
                os.unlink(editor_script)
        finally:
            if original_editor is None:
                os.environ.pop("EDITOR", None)
            else:
                os.environ["EDITOR"] = original_editor

    def test_returns_none_when_unchanged(self) -> None:
        original_editor = os.environ.get("EDITOR")
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".sh", delete=False, prefix="noop-editor.",
            ) as script:
                script.write('#!/bin/sh\n: $1\n')
                editor_script = script.name
            os.chmod(editor_script, 0o755)
            os.environ["EDITOR"] = editor_script
            try:
                result = supervise_cli.edit_in_editor("original")
                self.assertIsNone(result)
            finally:
                os.unlink(editor_script)
        finally:
            if original_editor is None:
                os.environ.pop("EDITOR", None)
            else:
                os.environ["EDITOR"] = original_editor


class TestResolveOrchestratorUrl(unittest.TestCase):
    """`_resolve_orchestrator_url` starts the backend orchestrator on demand
    when discovery finds nothing — supervise is often the first thing run."""

    def test_returns_discovered_url_without_starting(self) -> None:
        with patch.object(
            supervise_cli, "discover_orchestrator_url",
            return_value="http://127.0.0.1:8099",
        ), patch("bot_bottle.backend.get_bottle_backend") as get_backend:
            url = supervise_cli._resolve_orchestrator_url()
        self.assertEqual(url, "http://127.0.0.1:8099")
        get_backend.assert_not_called()  # nothing to start; discovery won

    def test_starts_backend_orchestrator_when_none_running(self) -> None:
        backend = MagicMock()
        backend.name = "firecracker"
        backend.ensure_orchestrator.return_value = "http://10.243.255.1:8099"
        with patch.object(
            supervise_cli, "discover_orchestrator_url",
            side_effect=supervise_cli.OrchestratorClientError("none"),
        ), patch("bot_bottle.backend.get_bottle_backend", return_value=backend):
            url = supervise_cli._resolve_orchestrator_url()
        self.assertEqual(url, "http://10.243.255.1:8099")
        backend.ensure_orchestrator.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
