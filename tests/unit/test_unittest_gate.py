"""Unit tests for CI's unittest execution-count gate."""

from __future__ import annotations

import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import Mock, patch

from scripts.unittest_gate import assurance_errors, main


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

    def test_main_accepts_successful_assured_suite(self) -> None:
        result = Mock(
            testsRun=22,
            skipped=[],
            wasSuccessful=Mock(return_value=True),
        )
        runner = Mock()
        runner.run.return_value = result
        with patch(
            "scripts.unittest_gate.unittest.defaultTestLoader.discover",
            return_value=Mock(),
        ) as discover, patch(
            "scripts.unittest_gate.unittest.TextTestRunner",
            return_value=runner,
        ) as runner_type:
            self.assertEqual(
                0,
                main([
                    "-s", "tests/integration",
                    "-t", ".",
                    "-p", "test_*.py",
                    "--minimum-executed", "22",
                    "--fail-on-skip",
                    "-v",
                ]),
            )
        discover.assert_called_once_with(
            "tests/integration", pattern="test_*.py", top_level_dir="."
        )
        runner_type.assert_called_once_with(verbosity=2)

    def test_main_rejects_unsuccessful_underfilled_suite(self) -> None:
        result = Mock(
            testsRun=1,
            skipped=[(Mock(), "not available")],
            wasSuccessful=Mock(return_value=False),
        )
        runner = Mock()
        runner.run.return_value = result
        error = StringIO()
        with patch(
            "scripts.unittest_gate.unittest.defaultTestLoader.discover",
            return_value=Mock(),
        ), patch(
            "scripts.unittest_gate.unittest.TextTestRunner",
            return_value=runner,
        ), redirect_stderr(error):
            self.assertEqual(
                1,
                main(["--minimum-executed", "2", "--fail-on-skip"]),
            )
        self.assertIn("below required minimum", error.getvalue())
        self.assertIn("skipped in a no-skip suite", error.getvalue())


if __name__ == "__main__":
    unittest.main()
