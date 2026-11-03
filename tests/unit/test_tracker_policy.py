import os
import unittest
from unittest import mock
from unittest.mock import Mock

from scripts.tracker_policy import (
    TRIAGE_LABEL,
    check_pull_request,
    deliberate_issue_numbers,
    ensure_issue_label,
)


class TestDeliberateIssueNumbers(unittest.TestCase):
    def test_accepts_completing_and_noncompleting_forms(self):
        self.assertEqual(
            deliberate_issue_numbers("Fixes #12", "Part of #14; refs #15"),
            {12, 14, 15},
        )

    def test_does_not_treat_incidental_number_as_link(self):
        self.assertEqual(deliberate_issue_numbers("Audit #12", "See PR #14"), set())


class TestCheckPullRequest(unittest.TestCase):
    def test_accepts_unlabelled_pr_linked_to_real_issue(self):
        api = Mock()
        api.request.return_value = {"number": 12, "pull_request": None}
        event = {"pull_request": {"title": "Change", "body": "Part of #12", "labels": []}}
        self.assertEqual(check_pull_request(event, api), [])

    def test_accepts_labelled_pr_without_issue(self):
        api = Mock()
        event = {
            "pull_request": {
                "title": "Change",
                "body": "",
                "labels": [{"name": "Kind/Documentation"}],
            }
        }
        self.assertEqual(check_pull_request(event, api), [])
        api.request.assert_not_called()

    def test_rejects_unlabelled_pr_without_issue(self):
        api = Mock()
        event = {"pull_request": {"title": "Change", "body": "", "labels": []}}
        errors = check_pull_request(event, api)
        self.assertEqual(len(errors), 1)
        self.assertIn("either have a label or reference an issue", errors[0])
        api.request.assert_not_called()

    def test_rejects_labelled_pr_linked_to_real_issue(self):
        api = Mock()
        api.request.return_value = {"number": 12, "pull_request": None}
        event = {
            "pull_request": {
                "title": "Change",
                "body": "Closes #12",
                "labels": [{"name": "Kind/Documentation"}],
            }
        }
        errors = check_pull_request(event, api)
        self.assertEqual(len(errors), 1)
        self.assertIn("exactly one tracking mode", errors[0])

    def test_still_validates_issue_reference_when_both_modes_are_used(self):
        api = Mock()
        api.request.return_value = {"number": 12, "pull_request": {}}
        event = {
            "pull_request": {
                "title": "Change",
                "body": "Closes #12",
                "labels": [{"name": "Kind/Bug"}],
            }
        }
        errors = check_pull_request(event, api)
        self.assertEqual(len(errors), 2)
        self.assertIn("exactly one tracking mode", errors[0])
        self.assertIn("not an issue", errors[1])


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
