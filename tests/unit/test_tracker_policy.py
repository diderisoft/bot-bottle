import os
import unittest
from unittest import mock
from unittest.mock import Mock

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
