import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from scripts.check_prd_numbers import (
    REPO_ROOT,
    _prd_names_in_ref,
    check_prd_numbers,
    main,
    rerun_open_prs,
)


def _write_prds(root: Path, *names: str) -> None:
    directory = root / "docs" / "prds"
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text("# PRD\n", encoding="utf-8")


class TestCheckPrdNumbers(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bb-prd-numbers.")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_accepts_distinctly_numbered_prds(self):
        _write_prds(self.root, "0001-first.md", "0002-second.md", "README.md")
        self.assertEqual(check_prd_numbers(self.root), [])

    def test_accepts_missing_prd_directory(self):
        self.assertEqual(check_prd_numbers(self.root), [])

    def test_rejects_unnumbered_placeholder(self):
        _write_prds(self.root, "0001-first.md", "prd-new-second.md")
        problems = check_prd_numbers(self.root)
        self.assertEqual(len(problems), 1)
        self.assertIn("prd-new-second.md", problems[0])
        self.assertIn("unnumbered", problems[0])

    def test_rejects_name_without_a_four_digit_prefix(self):
        _write_prds(self.root, "12-short.md")
        self.assertIn("unnumbered", check_prd_numbers(self.root)[0])

    def test_rejects_two_files_claiming_one_number_in_the_same_tree(self):
        _write_prds(self.root, "0007-first.md", "0007-second.md")
        problems = check_prd_numbers(self.root)
        self.assertEqual(len(problems), 1)
        self.assertIn("0007-first.md, 0007-second.md", problems[0])

    def test_counts_archived_prds(self):
        _write_prds(self.root, "0007-first.md", "archive/0007-old.md", "archive/README.md")
        problems = check_prd_numbers(self.root)
        self.assertEqual(len(problems), 1)
        self.assertIn("0007-first.md, 0007-old.md", problems[0])

    def test_rejects_number_already_committed_on_the_base_ref(self):
        _write_prds(self.root, "0082-mine.md")
        with patch(
            "scripts.check_prd_numbers._prd_names_in_ref", return_value=["0082-theirs.md"]
        ):
            problems = check_prd_numbers(self.root, base_ref="origin/main")
        self.assertEqual(len(problems), 1)
        self.assertIn("0082-mine.md, 0082-theirs.md", problems[0])

    def test_accepts_a_prd_unchanged_since_the_base_ref(self):
        _write_prds(self.root, "0082-same.md")
        with patch(
            "scripts.check_prd_numbers._prd_names_in_ref", return_value=["0082-same.md"]
        ):
            self.assertEqual(check_prd_numbers(self.root, base_ref="origin/main"), [])

    def test_ignores_unnumbered_names_that_only_exist_on_the_base_ref(self):
        _write_prds(self.root, "0001-first.md")
        with patch(
            "scripts.check_prd_numbers._prd_names_in_ref", return_value=["prd-new-theirs.md"]
        ):
            self.assertEqual(check_prd_numbers(self.root, base_ref="origin/main"), [])


class TestPrdNamesInRef(unittest.TestCase):
    def test_reads_committed_prd_names_from_a_real_ref(self):
        # Guards the git plumbing the base-ref comparison depends on.
        names = _prd_names_in_ref(REPO_ROOT, "HEAD")
        self.assertIn("README.md", names)
        self.assertTrue(any(name.startswith("0001-") for name in names))

    def test_raises_for_an_unknown_ref(self):
        # Fail closed: the base comparison is the whole point of the check, so an
        # unreadable base ref must fail it rather than silently pass.
        with self.assertRaises(RuntimeError):
            _prd_names_in_ref(REPO_ROOT, "refs/heads/no-such-branch")


Payload = dict[str, Any]


def _pull(number: int, sha: str) -> Payload:
    return {"number": number, "head": {"sha": sha}}


def _run(
    run_id: int,
    path: str = "prd-number-check.yml@refs/pull/1/head",
    status: str = "completed",
) -> Payload:
    return {"id": run_id, "path": path, "status": status}


class TestRerunOpenPrs(unittest.TestCase):
    def _api(self, pulls: list[Payload], runs_by_sha: dict[str, list[Payload]]) -> Mock:
        api = Mock()

        def request(method: str, path: str, payload: object = None) -> Any:
            # pylint: disable=unused-argument
            if path.startswith("/pulls"):
                return pulls if "page=1&" in path else []
            if path.startswith("/actions/runs?"):
                sha = path.split("head_sha=", 1)[1].split("&", 1)[0]
                # Real Gitea stamps each run's head_sha; the caller filters on it.
                runs = [{**r, "head_sha": sha} for r in runs_by_sha.get(sha, [])]
                return {"workflow_runs": runs}
            return None

        api.request.side_effect = request
        return api

    def test_reruns_the_latest_matching_run_per_pull_request(self):
        api = self._api(
            [_pull(1, "aaa"), _pull(2, "bbb")],
            {"aaa": [_run(10), _run(11)], "bbb": [_run(20)]},
        )
        report = rerun_open_prs(api)
        self.assertEqual(report, ["#1: re-ran 11", "#2: re-ran 20"])
        rerun_calls = [c.args[1] for c in api.request.call_args_list if c.args[0] == "POST"]
        self.assertEqual(rerun_calls, ["/actions/runs/11/rerun", "/actions/runs/20/rerun"])

    def test_ignores_runs_of_other_workflows(self):
        api = self._api([_pull(1, "aaa")], {"aaa": [_run(10, "test.yml@refs/pull/1/head")]})
        self.assertEqual(rerun_open_prs(api), ["#1: no prd-number-check.yml run for aaa, skipped"])

    def test_skips_a_run_that_has_not_finished(self):
        api = self._api([_pull(1, "aaa")], {"aaa": [_run(10, status="in_progress")]})
        self.assertEqual(rerun_open_prs(api), ["#1: run 10 is still in_progress, skipped"])

    def test_reports_a_failed_rerun_and_continues(self):
        api = self._api([_pull(1, "aaa"), _pull(2, "bbb")], {"aaa": [_run(10)], "bbb": [_run(20)]})
        inner = api.request.side_effect

        def request(method: str, path: str, payload: object = None) -> Any:
            if method == "POST" and path == "/actions/runs/10/rerun":
                error = urllib.error.HTTPError(path, 403, "Forbidden", {}, None)  # type: ignore
                self.addCleanup(error.close)
                raise error
            return inner(method, path, payload)

        api.request.side_effect = request
        report = rerun_open_prs(api)
        self.assertEqual(report, ["#1: rerun of 10 failed (403 Forbidden)", "#2: re-ran 20"])

    def test_ignores_a_run_whose_head_sha_does_not_match(self):
        # If the server ignores the head_sha query and hands back another PR's
        # run, the client-side head_sha filter must exclude it, not re-run it.
        api = Mock()

        def request(method: str, path: str, payload: object = None) -> Any:
            if path.startswith("/pulls"):
                return [_pull(1, "aaa")] if "page=1&" in path else []
            if path.startswith("/actions/runs?"):
                return {"workflow_runs": [{**_run(10), "head_sha": "zzz"}]}
            return None

        api.request.side_effect = request
        self.assertEqual(
            rerun_open_prs(api), ["#1: no prd-number-check.yml run for aaa, skipped"])

    def test_reports_nothing_when_no_pull_requests_are_open(self):
        self.assertEqual(rerun_open_prs(self._api([], {})), [])


class TestMain(unittest.TestCase):
    def test_check_exits_zero_when_the_tree_is_clean(self):
        with patch("sys.argv", ["check_prd_numbers.py", "check"]), patch(
            "scripts.check_prd_numbers.check_prd_numbers", return_value=[]
        ), patch("builtins.print"):
            self.assertEqual(main(), 0)

    def test_check_exits_nonzero_and_annotates_each_problem(self):
        argv = ["check_prd_numbers.py", "check", "--base-ref", "origin/main"]
        with patch("sys.argv", argv), patch(
            "scripts.check_prd_numbers.check_prd_numbers", return_value=["0082: taken"]
        ), patch("builtins.print") as printed:
            self.assertEqual(main(), 1)
        printed.assert_called_once_with("::error::0082: taken")

    def test_rerun_command_prefers_an_explicit_pat(self):
        env = {
            "GITHUB_API_URL": "https://gitea.example/api/v1",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_TOKEN": "run-scoped",
            "PRD_RERUN_TOKEN": "pat",
        }
        with patch("sys.argv", ["check_prd_numbers.py", "rerun-open-prs"]), patch.dict(
            os.environ, env, clear=True
        ), patch("scripts.check_prd_numbers.rerun_open_prs", return_value=[]) as rerun, patch(
            "builtins.print"
        ):
            self.assertEqual(main(), 0)
        self.assertEqual(rerun.call_args.args[0].token, "pat")

    def test_rerun_command_falls_back_to_the_run_scoped_token(self):
        env = {
            "GITHUB_API_URL": "https://gitea.example/api/v1",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_TOKEN": "run-scoped",
            "PRD_RERUN_TOKEN": "",
        }
        with patch("sys.argv", ["check_prd_numbers.py", "rerun-open-prs"]), patch.dict(
            os.environ, env, clear=True
        ), patch(
            "scripts.check_prd_numbers.rerun_open_prs", return_value=["#1: re-ran 9"]
        ) as rerun, patch("builtins.print"):
            self.assertEqual(main(), 0)
        self.assertEqual(rerun.call_args.args[0].token, "run-scoped")


if __name__ == "__main__":
    unittest.main()
