"""Unit tests for CI's unittest execution-count gate."""

from __future__ import annotations

import unittest

from scripts.unittest_gate import assurance_errors


class TestAssuranceErrors(unittest.TestCase):
    def test_accepts_suite_that_meets_minimum_without_skips(self) -> None:
        self.assertEqual(
            [],
            assurance_errors(
                tests_run=22, skipped=0, minimum_executed=22, fail_on_skip=True
            ),
        )

    def test_rejects_green_suite_below_execution_minimum(self) -> None:
        errors = assurance_errors(
            tests_run=22, skipped=18, minimum_executed=22, fail_on_skip=False
        )
        self.assertEqual(1, len(errors))
        self.assertIn("executed 4", errors[0])

    def test_rejects_any_skip_when_required(self) -> None:
        errors = assurance_errors(
            tests_run=23, skipped=1, minimum_executed=22, fail_on_skip=True
        )
        self.assertEqual(["1 test(s) skipped in a no-skip suite"], errors)


if __name__ == "__main__":
    unittest.main()
