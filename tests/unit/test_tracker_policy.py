import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import Mock

from scripts import tracker_policy
from scripts.tracker_policy import (
    TRIAGE_LABEL,
    ensure_issue_label,
)


class TestEnsureIssueLabel(unittest.TestCase):
    def setUp(self) -> None:
        # The CI runner's own variables mustn't decide which forge this is.
        env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_ACTIONS", "GITEA_ACTIONS")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_on_github_the_label_goes_by_name(self):
        api = Mock()
        api.request.side_effect = [[{"id": 55, "name": TRIAGE_LABEL}], None]
        event = {"issue": {"number": 405, "labels": [], "pull_request": None}}
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            ensure_issue_label(event, api)
        api.request.assert_any_call("POST", "/issues/405/labels", {"labels": [TRIAGE_LABEL]})

    def test_on_gitea_the_label_goes_by_id_though_github_actions_is_set(self):
        api = Mock()
        api.request.side_effect = [[{"id": 55, "name": TRIAGE_LABEL}], None]
        event = {"issue": {"number": 405, "labels": [], "pull_request": None}}
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITEA_ACTIONS": "true"}):
            ensure_issue_label(event, api)
        api.request.assert_any_call("POST", "/issues/405/labels", {"labels": [55]})

    def test_adds_triage_label_to_unlabelled_issue(self):
        api = Mock()
        api.request.side_effect = [[{"id": 55, "name": TRIAGE_LABEL}], None]
        event = {"issue": {"number": 405, "labels": [], "pull_request": None}}
        self.assertTrue(ensure_issue_label(event, api))
        api.request.assert_any_call("POST", "/issues/405/labels", {"labels": [55]})

    def test_leaves_labelled_issue_unchanged(self):
        api = Mock()
        event = {"issue": {"number": 405, "labels": [{"name": "Kind/Documentation"}]}}
        self.assertFalse(ensure_issue_label(event, api))
        api.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class TestMain(unittest.TestCase):
    def setUp(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.event = Path(tmp) / "event.json"
        self.event.write_text(json.dumps(
            {"issue": {"number": 405, "labels": [], "pull_request": None}}), encoding="utf-8")
        env = {
            "GITHUB_API_URL": "https://api.example.test",
            "GITHUB_REPOSITORY": "acme/app",
            "GITHUB_TOKEN": "t",
            "GITHUB_EVENT_PATH": str(self.event),
        }
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_label_issue_labels_the_event_issue(self):
        with mock.patch.object(tracker_policy, "GiteaApi") as api_cls, \
                mock.patch.object(sys, "argv", ["tracker_policy.py", "label-issue"]), \
                mock.patch("builtins.print") as printed:
            api_cls.return_value.request.side_effect = [[{"id": 55, "name": TRIAGE_LABEL}], None]
            self.assertEqual(tracker_policy.main(), 0)
        api_cls.assert_called_once_with("https://api.example.test", "acme/app", "t")
        printed.assert_called_once_with(f"Applied {TRIAGE_LABEL}.")

    def test_check_pr_is_no_longer_a_command(self):
        with mock.patch.object(sys, "argv", ["tracker_policy.py", "check-pr"]), \
                mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            tracker_policy.main()
