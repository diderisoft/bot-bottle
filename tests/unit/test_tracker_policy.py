import unittest
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

    def test_rejects_labels_and_pr_reference(self):
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
        self.assertIn("unlabeled", errors[0])
        self.assertIn("not an issue", errors[1])


class TestEnsureIssueLabel(unittest.TestCase):
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
