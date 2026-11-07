"""Unit: the `Bot-Bottle` commit trailer (PRD 0112 step 1).

The guest hooks that add it, chained to a repo's own hooks, and git-gate's
pre-receive check, both exercised against real git repos in a temp dir."""

from __future__ import annotations

import dataclasses
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bot_bottle.backend.resolve_common import launch_trailer, with_commit_hooks_env
from bot_bottle.contract.git_trailers import (
    TRAILER_KEY, trailer_mode, trailer_value,
)
from bot_bottle.control.git_gate_render import (
    git_gate_render_hook, git_gate_render_provision,
)
from bot_bottle.gateway.git_gate import audit, trailer_check
from bot_bottle.git_gate import GitGate, commit_hooks
from bot_bottle.git_gate.service import (
    load_baseline, load_trailer_settings, save_baseline, workspace_baseline,
)
from bot_bottle.manifest import ManifestIndex
from bot_bottle.manifest.document import bottle_document
from bot_bottle.manifest.util import ManifestError
from tests.unit.test_docker_provision_git_user import _PROVIDER, _make_bottle, _plan
from tests.unit.test_git_gate_audit import _Orchestrator

REPO_ROOT = Path(__file__).resolve().parents[2]
VALUE = "dev+gitea/demo-abc12"
BASELINE_B1 = "/git-gate/creds/b1/baseline-commits"


def _bottles(raw: dict[str, object]) -> ManifestIndex:
    return ManifestIndex.from_json_obj({
        "bottles": raw,
        "agents": {"demo": {"skills": [], "prompt": ""}},
    })


class TestTrailerContract(unittest.TestCase):
    def test_value_joins_bottles_and_run(self):
        self.assertEqual(VALUE, trailer_value(("dev", "gitea"), "demo-abc12"))

    def test_value_reduces_unsafe_characters(self):
        self.assertEqual("site/run-1", trailer_value(("./site",), "run 1"))

    def test_value_needs_both_parts(self):
        for names, run in (((), "r"), (("dev",), ""), (("..",), "r")):
            with self.subTest(names=names, run=run), self.assertRaises(ValueError):
                trailer_value(names, run)

    def test_mode_default_and_rejects_unknown(self):
        self.assertEqual("require", trailer_mode(""))
        self.assertEqual("warn", trailer_mode("warn"))
        with self.assertRaises(ValueError):
            trailer_mode("sometimes")


class TestManifestTrailers(unittest.TestCase):
    def test_unset_is_empty(self):
        self.assertEqual("", _bottles({"dev": {}}).bottles["dev"].git_trailers)

    def test_parses_each_mode(self):
        for mode in ("require", "warn", "off"):
            with self.subTest(mode=mode):
                b = _bottles({"dev": {"git-gate": {"trailers": mode}}}).bottles["dev"]
                self.assertEqual(mode, b.git_trailers)

    def test_rejects_unknown_mode(self):
        with self.assertRaises(ManifestError):
            _bottles({"dev": {"git-gate": {"trailers": "maybe"}}})

    def test_extends_child_wins_else_inherits(self):
        idx = _bottles({
            "base": {"git-gate": {"trailers": "warn"}},
            "dev": {"extends": "base"},
            "strict": {"extends": "base", "git-gate": {"trailers": "require"}},
        })
        self.assertEqual("warn", idx.bottles["dev"].git_trailers)
        self.assertEqual("require", idx.bottles["strict"].git_trailers)

    def test_document_round_trip(self):
        b = _bottles({"dev": {"git-gate": {"trailers": "off"}}}).bottles["dev"]
        self.assertEqual({"trailers": "off"}, bottle_document(b)["git-gate"])


class TestLaunchWiring(unittest.TestCase):
    def test_launch_trailer_uses_selected_bottles_else_basic(self):
        plan = _plan()
        self.assertEqual("dev/demo-abc12", launch_trailer(plan.spec, "demo-abc12"))
        spec = dataclasses.replace(plan.spec, bottle_names=("dev", "gitea"))
        self.assertEqual(VALUE, launch_trailer(spec, "demo-abc12"))
        spec = dataclasses.replace(plan.spec, bottle_names=())
        self.assertEqual("basic/demo-abc12", launch_trailer(spec, "demo-abc12"))

    def test_hooks_env_points_hookspath_at_the_hooks(self):
        provision = with_commit_hooks_env(_plan().agent_provision)
        env = provision.guest_env
        self.assertEqual("1", env["GIT_CONFIG_COUNT"])
        self.assertEqual("core.hooksPath", env["GIT_CONFIG_KEY_0"])
        self.assertEqual("/home/node/.config/git/bot-bottle-hooks", env["GIT_CONFIG_VALUE_0"])
        self.assertLessEqual({"GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0"},
                             provision.hidden_env_names)

    def test_hooks_env_appends_after_existing_entries(self):
        env = commit_hooks.hooks_env("/home/node", {"GIT_CONFIG_COUNT": "2"})
        self.assertEqual({"GIT_CONFIG_COUNT": "3", "GIT_CONFIG_KEY_2": "core.hooksPath",
                          "GIT_CONFIG_VALUE_2": "/home/node/.config/git/bot-bottle-hooks"},
                         env)

    def test_prepare_saves_settings_for_reprovision(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            bottle = _bottles({"dev": {"git-gate": {"trailers": "warn"}}}).bottles["dev"]
            plan = GitGate().prepare(bottle, "demo-abc12", stage, trailer=VALUE)
            self.assertEqual((VALUE, "warn"), (plan.trailer, plan.trailer_mode))
            self.assertEqual((VALUE, "warn"), load_trailer_settings(stage))
            default = GitGate().prepare(_bottles({"dev": {}}).bottles["dev"], "s", stage,
                                        trailer=VALUE)
            self.assertEqual("require", default.trailer_mode)

    def test_provision_render_sets_repo_config(self):
        script = git_gate_render_provision("b1", (), trailer_mode="require", trailer=VALUE,
                                           baseline=BASELINE_B1)
        self.assertIn("trailers_mode=require", script)
        self.assertIn(f"trailer={VALUE}", script)
        self.assertIn(f"baseline={BASELINE_B1}", script)
        for key, var in (("trailers", "trailers_mode"), ("trailer", "trailer"),
                         ("baseline", "baseline")):
            self.assertIn(f'config git-gate.{key} "${var}"', script)

    def test_provision_render_rejects_bad_settings(self):
        for kwargs in ({"trailer_mode": "bogus", "trailer": VALUE, "baseline": BASELINE_B1},
                       {"trailer_mode": "require", "trailer": "a/b; rm -rf /",
                        "baseline": BASELINE_B1},
                       {"trailer_mode": "require", "trailer": VALUE, "baseline": "/etc/passwd"},
                       {"trailer_mode": "require", "trailer": VALUE, "baseline": ""}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                git_gate_render_provision("b1", (), **kwargs)

    def test_pre_receive_checks_trailers_before_gitleaks(self):
        hook = git_gate_render_hook()
        self.assertIn("bot_bottle.gateway.git_gate.trailer_check", hook)
        self.assertLess(hook.index("trailer_check"), hook.index("gitleaks scanning"))
        self.assertGreater(hook.index("trailer_check"), hook.index("refusing AGit review ref"))

    def test_provision_commit_hooks_installs_dispatcher_and_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = _plan(stage_dir=Path(tmp))
            plan = dataclasses.replace(plan, git_gate_plan=dataclasses.replace(
                plan.git_gate_plan, trailer=VALUE, trailer_mode="require"))
            bottle = _make_bottle()
            with patch.object(commit_hooks, "operator_identity",
                              return_value=("Op Erator", "op@example.com")):
                _PROVIDER.provision_commit_hooks(bottle, plan)
            copied = [c.args[1] for c in bottle.cp_in.call_args_list]
            directory = "/home/node/.config/git/bot-bottle-hooks"
            self.assertEqual([f"{directory}/bot-bottle-hook",
                              f"{directory}/bot-bottle.gitconfig"], copied)
            config = (Path(tmp) / "bot-bottle.gitconfig").read_text()
            self.assertIn(f'trailer = "{VALUE}"', config)
            self.assertIn('operatorEmail = "op@example.com"', config)
            install = bottle.exec.call_args_list[-1].args[0]
            self.assertIn("ln -sf bot-bottle-hook", install)


class TestOperatorIdentity(unittest.TestCase):
    def _run(self, values: dict[str, str]):
        def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            value = values.get(argv[-1], "")
            return subprocess.CompletedProcess(argv, 0 if value else 1, value + "\n", "")
        return run

    def test_reads_host_global_identity(self):
        run = self._run({"user.name": "Op", "user.email": "op@example.com"})
        self.assertEqual(("Op", "op@example.com"), commit_hooks.operator_identity(run))

    def test_empty_when_either_is_unset(self):
        self.assertEqual(("", ""), commit_hooks.operator_identity(self._run({"user.name": "Op"})))

    def test_empty_when_git_is_missing(self):
        def run(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("git")
        self.assertEqual(("", ""), commit_hooks.operator_identity(run))

    def test_config_rejects_identity_injection(self):
        with self.assertRaises(ValueError):
            commit_hooks.render_hook_config(VALUE, "Op\n[core]", "op@example.com")
        self.assertNotIn("operator", commit_hooks.render_hook_config(VALUE, "Op", ""))


class _GitCase(unittest.TestCase):
    """A temp dir with git isolated from the host's config."""

    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git not installed")
        self._tmp = tempfile.TemporaryDirectory(prefix="bb-trailers.")  # pylint: disable=consider-using-with
        self.root = Path(self._tmp.name)
        home = self.root / "home"
        home.mkdir()
        self.env = {
            "PATH": os.environ.get("PATH", ""), "HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
            "GIT_AUTHOR_NAME": "Bot", "GIT_AUTHOR_EMAIL": "bot@example.com",
            "GIT_COMMITTER_NAME": "Bot", "GIT_COMMITTER_EMAIL": "bot@example.com",
            "TMPDIR": str(self.root),
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def git(self, *args: str, cwd: Path, check: bool = True,
            env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(["git", *args], cwd=cwd, env={**self.env, **(env or {})},
                              capture_output=True, text=True, check=False)
        if check and proc.returncode != 0:
            self.fail(f"git {' '.join(args)} failed: {proc.stderr}")
        return proc

    def install_hooks(self, operator: tuple[str, str] = ("", "")) -> Path:
        hooks = self.root / "hooks"
        hooks.mkdir()
        (hooks / commit_hooks.DISPATCHER_NAME).write_text(commit_hooks.render_dispatcher())
        (hooks / commit_hooks.CONFIG_NAME).write_text(
            commit_hooks.render_hook_config(VALUE, *operator))
        subprocess.run(["sh", "-c", commit_hooks.render_install(
            str(hooks), owner=f"{os.getuid()}:{os.getgid()}")], check=True)
        self.env.update(commit_hooks.hooks_env(str(self.root), {}))
        self.env["GIT_CONFIG_VALUE_0"] = str(hooks)
        return hooks

    def body(self, repo: Path, rev: str = "HEAD") -> str:
        return self.git("log", "-1", "--format=%B", rev, cwd=repo).stdout

    def trailers(self, repo: Path, rev: str = "HEAD") -> list[str]:
        out = self.git("log", "-1", "--format=%(trailers:only,unfold)", rev, cwd=repo).stdout
        return [line for line in out.splitlines() if line]


class TestCommitHooksChaining(_GitCase):
    def setUp(self) -> None:
        super().setUp()
        self.install_hooks(operator=("Op Erator", "op@example.com"))
        self.repo = self.root / "repo"
        self.git("init", "-q", str(self.repo), cwd=self.root)
        githooks = self.repo / ".githooks"
        githooks.mkdir()
        msg_hook = githooks / "commit-msg"
        msg_hook.write_text(
            "#!/bin/sh\n"
            "echo \"repo commit-msg ran: $(head -1 \"$1\")\" >&2\n"
            "grep -q '^feat' \"$1\" || { echo 'rejected: not feat' >&2; exit 3; }\n")
        msg_hook.chmod(0o755)
        self.git("config", "core.hooksPath", ".githooks", cwd=self.repo)

    def commit(self, *args: str, check: bool = True,
               env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return self.git("commit", "-q", "--allow-empty", *args, cwd=self.repo, check=check,
                        env=env)

    def test_repo_hook_still_rejects_with_its_exit_code(self):
        proc = self.commit("-m", "bad message", check=False)
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("rejected: not feat", proc.stderr)
        self.assertEqual(128, self.git("rev-parse", "HEAD", cwd=self.repo,
                                       check=False).returncode)

    def test_adds_trailers_and_runs_repo_hook(self):
        proc = self.commit("-m", "feat: x", "-m",
                           "Co-Authored-By: Claude <noreply@anthropic.com>")
        self.assertIn("repo commit-msg ran: feat: x", proc.stderr)
        self.assertEqual([
            "Co-Authored-By: Claude <noreply@anthropic.com>",
            f"{TRAILER_KEY}: {VALUE}",
            "Co-Authored-By: Op Erator <op@example.com>",
        ], self.trailers(self.repo))

    def test_amend_never_duplicates(self):
        self.commit("-m", "feat: x")
        self.commit("--amend", "--no-edit")
        self.commit("--amend", "-m", "feat: y", "--trailer", f"{TRAILER_KEY}: other/run")
        self.assertEqual([f"{TRAILER_KEY}: {VALUE}",
                          "Co-Authored-By: Op Erator <op@example.com>"],
                         self.trailers(self.repo))

    def test_no_co_author_when_operator_is_the_author(self):
        self.commit("-m", "feat: mine", env={"GIT_AUTHOR_EMAIL": "OP@example.com"})
        self.assertEqual([f"{TRAILER_KEY}: {VALUE}"], self.trailers(self.repo))

    def test_merge_commit_gets_trailer(self):
        self.commit("-m", "feat: base")
        self.git("checkout", "-q", "-b", "side", cwd=self.repo)
        self.commit("-m", "feat: side")
        self.git("checkout", "-q", "-", cwd=self.repo)
        self.commit("-m", "feat: main")
        self.git("merge", "-q", "--no-ff", "-m", "feat: merge", "side", cwd=self.repo)
        self.assertIn(f"{TRAILER_KEY}: {VALUE}", self.trailers(self.repo))

    def test_default_hooks_dir_chains_when_repo_sets_no_hookspath(self):
        self.git("config", "--unset", "core.hooksPath", cwd=self.repo)
        pre_commit = self.repo / ".git" / "hooks" / "pre-commit"
        pre_commit.write_text("#!/bin/sh\necho 'default pre-commit ran' >&2\nexit 7\n")
        pre_commit.chmod(0o755)
        proc = self.commit("-m", "anything", check=False)
        self.assertEqual(1, proc.returncode)
        self.assertIn("default pre-commit ran", proc.stderr)
        pre_commit.write_text("#!/bin/sh\nexit 0\n")
        self.commit("-m", "anything")
        self.assertIn(f"{TRAILER_KEY}: {VALUE}", self.trailers(self.repo))

    def test_repo_hook_gets_args_and_stdin(self):
        post_rewrite = self.repo / ".githooks" / "post-rewrite"
        out = self.root / "post-rewrite.out"
        post_rewrite.write_text(f"#!/bin/sh\necho \"$1\" > {out}\ncat >> {out}\n")
        post_rewrite.chmod(0o755)
        self.commit("-m", "feat: x")
        self.commit("--amend", "--no-edit")
        lines = out.read_text().splitlines()
        self.assertEqual("amend", lines[0])
        self.assertEqual(2, len(lines[1].split()))

    def test_without_hooks_env_repo_config_is_used(self):
        # The override is what routes git through the dispatcher.
        env = {"GIT_CONFIG_COUNT": "0"}
        self.git("commit", "-q", "--allow-empty", "-m", "feat: plain", cwd=self.repo, env=env)
        self.assertEqual([], self.trailers(self.repo))


class _GateCase(_GitCase):
    """A bare gate repo whose pre-receive runs only the trailer check."""

    def setUp(self) -> None:
        super().setUp()
        self.gate = self.root / "gate.git"
        self.git("init", "-q", "--bare", str(self.gate), cwd=self.root)
        hook = self.gate / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\n"
            "refs=$(mktemp)\ncat > \"$refs\"\n"
            f"PYTHONPATH={REPO_ROOT} {sys.executable} "
            "-m bot_bottle.gateway.git_gate.trailer_check \"$refs\"\n")
        hook.chmod(0o755)
        self.state = self.root / "state"
        self.state.mkdir()
        self.baseline = save_baseline(self.state, [])
        self.git("config", "git-gate.baseline", str(self.baseline), cwd=self.gate)
        self.set_mode("require")
        self.work = self.root / "work"
        self.git("init", "-q", "-b", "main", str(self.work), cwd=self.root)
        self.git("remote", "add", "origin", str(self.gate), cwd=self.work)
        # "Upstream" history: on the gate before the bottle pushes anything.
        self.git("commit", "-q", "--allow-empty", "-m", "upstream", cwd=self.work)
        self.set_mode("off")
        self.push()
        self.set_mode("require")

    def set_mode(self, mode: str) -> None:
        self.git("config", "git-gate.trailers", mode, cwd=self.gate)
        self.git("config", "git-gate.trailer", VALUE, cwd=self.gate)

    def push(self, *refs: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.git("push", "-q", "origin", *(refs or ("HEAD:refs/heads/main",)),
                        cwd=self.work, check=check)

    def commit(self, msg: str, *trailers: str) -> None:
        args = [a for t in trailers for a in ("--trailer", t)]
        self.git("commit", "-q", "--allow-empty", "-m", msg, *args, cwd=self.work)


class TestGateTrailerCheck(_GateCase):
    def test_accepts_commits_with_this_bottles_trailer(self):
        self.commit("feat: ok", f"{TRAILER_KEY}: {VALUE}")
        self.push()

    def test_refuses_missing_trailer_with_fix(self):
        self.commit("feat: bare")
        proc = self.push(check=False)
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("don't carry this bottle's trailer", proc.stderr)
        self.assertIn("no Bot-Bottle trailer", proc.stderr)
        self.assertIn(f'--trailer "{TRAILER_KEY}: {VALUE}"', proc.stderr)
        upstream = self.git("rev-parse", "--short=12", "HEAD~1", cwd=self.work).stdout.strip()
        self.assertIn("git rebase --rebase-merges --exec", proc.stderr)
        self.assertIn(upstream, proc.stderr)

    def test_fix_command_from_the_message_works(self):
        self.commit("feat: one")
        self.commit("feat: two", f"{TRAILER_KEY}: other/run")
        proc = self.push(check=False)
        fix = next(line.split("git-gate:   ", 1)[1] for line in proc.stderr.splitlines()
                   if "git rebase" in line)
        fixed = subprocess.run(["sh", "-c", fix], cwd=self.work, env=self.env, check=False,
                               capture_output=True, text=True)
        self.assertEqual(0, fixed.returncode, fixed.stderr)
        self.push()
        self.assertEqual([f"{TRAILER_KEY}: {VALUE}"], self.trailers(self.work))

    def test_refuses_another_bottles_trailer(self):
        self.commit("feat: other", f"{TRAILER_KEY}: dev/someone-else")
        proc = self.push(check=False)
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("names dev/someone-else", proc.stderr)

    def test_refuses_a_second_trailer_naming_another_bottle(self):
        self.commit("feat: both", f"{TRAILER_KEY}: {VALUE}", f"{TRAILER_KEY}: x/y")
        self.assertNotEqual(0, self.push(check=False).returncode)

    def test_checks_merge_commits_made_in_the_bottle(self):
        self.git("checkout", "-q", "-b", "side", cwd=self.work)
        self.commit("feat: side", f"{TRAILER_KEY}: {VALUE}")
        self.git("checkout", "-q", "main", cwd=self.work)
        self.commit("feat: main", f"{TRAILER_KEY}: {VALUE}")
        self.git("merge", "-q", "--no-ff", "-m", "merge side", "side", cwd=self.work)
        proc = self.push(check=False)
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("merge side", proc.stderr)

    def test_commits_already_on_the_gate_are_not_checked(self):
        # A new branch at upstream's tip introduces no commits.
        self.push("HEAD:refs/heads/topic")
        self.commit("feat: ok", f"{TRAILER_KEY}: {VALUE}")
        self.push("HEAD:refs/heads/topic")

    def test_warn_accepts_and_says_so(self):
        self.set_mode("warn")
        self.commit("feat: bare")
        proc = self.push()
        self.assertIn("warning refs/heads/main", proc.stderr)

    def test_off_accepts(self):
        self.commit("feat: bare")
        self.set_mode("off")
        self.push()

    def test_a_repo_missing_any_setting_is_refused_as_predating_trailers(self):
        self.commit("feat: ok", f"{TRAILER_KEY}: {VALUE}")
        for key in ("git-gate.trailers", "git-gate.trailer", "git-gate.baseline"):
            with self.subTest(key=key):
                value = self.git("config", "--get", key, cwd=self.gate).stdout.strip()
                self.git("config", "--unset", key, cwd=self.gate)
                proc = self.push(check=False)
                self.assertNotEqual(0, proc.returncode)
                self.assertIn(f"{key} is missing or invalid", proc.stderr)
                self.assertIn("Relaunch the bottle to reprovision it", proc.stderr)
                self.git("config", key, value, cwd=self.gate)

    def test_a_missing_baseline_file_is_refused(self):
        self.commit("feat: ok", f"{TRAILER_KEY}: {VALUE}")
        self.baseline.unlink()
        proc = self.push(check=False)
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("git-gate.baseline is missing or invalid", proc.stderr)
        self.set_mode("off")
        self.assertNotEqual(0, self.push(check=False).returncode)

    def test_deletion_is_not_checked(self):
        self.push("HEAD:refs/heads/topic")
        self.push(":refs/heads/topic")


class TestBaseline(_GateCase):
    """A workspace copied with an unpushed operator commit: that commit is in
    the baseline and pushes untouched; the agent's commits are checked."""

    def setUp(self) -> None:
        super().setUp()
        # The operator's local, unpushed commit, made before launch.
        self.commit("feat: operator work in progress")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD~1", cwd=self.work)
        baseline = commit_hooks_baseline(self.work)
        self.operator_sha = self.git("rev-parse", "HEAD", cwd=self.work).stdout.strip()
        self.assertEqual([self.operator_sha], baseline)
        save_baseline(self.state, baseline)

    def test_operator_commit_pushes_untouched_with_agent_commit(self):
        self.commit("feat: agent", f"{TRAILER_KEY}: {VALUE}")
        self.push()
        self.assertEqual(self.operator_sha, self.git(
            "rev-parse", "HEAD~1", cwd=self.work).stdout.strip())

    def test_agent_commit_without_trailer_is_refused_alone(self):
        self.commit("feat: agent bare")
        proc = self.push(check=False)
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("1 new commit(s)", proc.stderr)
        self.assertIn("feat: agent bare", proc.stderr)
        self.assertNotIn("operator work in progress", proc.stderr)
        # The fix rebases onto the operator's commit, leaving it alone.
        self.assertIn(self.operator_sha[:12], proc.stderr)

    def test_rewritten_baseline_commit_is_checked(self):
        self.git("commit", "-q", "--amend", "--allow-empty", "-m", "feat: reworded",
                 cwd=self.work)
        # The original is in the baseline but never reaches the gate.
        proc = self.push(check=False)
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("feat: reworded  (no Bot-Bottle trailer)", proc.stderr)

    def test_an_empty_baseline_checks_the_operator_commit(self):
        # No copied git workspace: the baseline is empty.
        self.baseline.write_text("", encoding="utf-8")
        self.commit("feat: agent", f"{TRAILER_KEY}: {VALUE}")
        proc = self.push(check=False)
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("operator work in progress", proc.stderr)


def commit_hooks_baseline(repo: Path) -> list[str]:
    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        return subprocess.run(argv, env=env, **kw)  # pylint: disable=subprocess-run-check
    return workspace_baseline(repo, run=run)


class TestBaselineFiles(unittest.TestCase):
    def test_save_unions_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            path = save_baseline(stage, [])
            self.assertEqual("", path.read_text(encoding="utf-8"))
            self.assertEqual([], trailer_check.read_baseline(str(path)))
            save_baseline(stage, ["a" * 40, "b" * 40])
            save_baseline(stage, ["b" * 40, "c" * 40])
            self.assertEqual(["a" * 40, "b" * 40, "c" * 40],
                             path.read_text(encoding="utf-8").split())
            self.assertEqual(path, load_baseline(stage))
            self.assertEqual(["a" * 40, "b" * 40, "c" * 40],
                             trailer_check.read_baseline(str(path)))

    def test_read_baseline_ignores_junk_and_fails_on_missing_files(self):
        self.assertIsNone(trailer_check.read_baseline(""))
        self.assertIsNone(trailer_check.read_baseline("/nonexistent/baseline"))
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
            f.write("not-a-sha\n" + "d" * 40 + "\n--all\n")
        try:
            self.assertEqual(["d" * 40], trailer_check.read_baseline(f.name))
        finally:
            os.unlink(f.name)

    def test_workspace_baseline_empty_when_git_fails(self):
        def run(argv: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 128, "", "not a repo")
        self.assertEqual([], workspace_baseline(Path("/x"), run=run))

    def test_prepare_always_writes_the_baseline(self):
        bottle = _bottles({"dev": {}}).bottles["dev"]
        with tempfile.TemporaryDirectory() as tmp:
            plan = GitGate().prepare(bottle, "s", Path(tmp), trailer=VALUE,
                                     baseline=["e" * 40])
            self.assertEqual(Path(tmp) / "baseline-commits", plan.baseline_file)
            self.assertEqual(plan.baseline_file, load_baseline(Path(tmp)))
        with tempfile.TemporaryDirectory() as tmp:
            plan = GitGate().prepare(bottle, "s", Path(tmp), trailer=VALUE)
            self.assertEqual("", plan.baseline_file.read_text(encoding="utf-8"))

    def test_prepare_rejects_an_unsafe_trailer(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            GitGate().prepare(_bottles({"dev": {}}).bottles["dev"], "s", Path(tmp),
                              trailer="a/b; rm -rf /")


class TestPushAuditRecord(_GitCase):
    """pre-receive's push_accepted event lists the commits a push forwards."""

    def setUp(self) -> None:
        super().setUp()
        self.server = _Orchestrator()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.gate = self.root / "gate.git"
        self.git("init", "-q", "--bare", str(self.gate), cwd=self.root)
        self.git("config", "git-gate.trailer", VALUE, cwd=self.gate)
        hook = self.gate / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\n"
            "while read -r old new ref; do\n"
            f"  PYTHONPATH={REPO_ROOT} {sys.executable} -m bot_bottle.gateway.git_gate.audit "
            "forge.push_accepted success \"$ref\" --findings 0 --new \"$new\" || exit 1\n"
            "done\n")
        hook.chmod(0o755)
        self.env.update({"BOT_BOTTLE_ORCHESTRATOR_URL":
                         f"http://127.0.0.1:{self.server.server_address[1]}",
                         "GIT_GATE_BOTTLE": "reg-id-1"})
        self.work = self.root / "work"
        self.git("init", "-q", "-b", "main", str(self.work), cwd=self.root)
        self.git("remote", "add", "origin", str(self.gate), cwd=self.work)

    def head(self) -> str:
        return self.git("rev-parse", "HEAD", cwd=self.work).stdout.strip()

    def test_each_forwarded_commit_is_recorded_once(self):
        self.git("commit", "-q", "--allow-empty", "-m", "one", cwd=self.work)
        first = self.head()
        self.git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=self.work)
        self.git("commit", "-q", "--allow-empty", "-m", "two", cwd=self.work)
        self.git("commit", "-q", "--allow-empty", "-m", "three", cwd=self.work)
        self.git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=self.work)
        events = [b["event"] for b in self.server.received]
        self.assertEqual(2, len(events))
        self.assertEqual([first], events[0]["payload"]["commits"])
        second = events[1]["payload"]
        self.assertEqual([self.git("rev-parse", "HEAD~1", cwd=self.work).stdout.strip(),
                          self.head()], second["commits"])
        self.assertEqual(2, second["commit_count"])
        self.assertEqual(VALUE, second["bot_bottle_trailer"])
        self.assertEqual("reg-id-1", events[1]["bottle"])
        self.assertTrue(events[1]["ts_event"])

    def test_build_event_without_new_lists_nothing(self):
        event = audit.build_event("forge.push_accepted", "success", "refs/heads/x",
                                  bottle="b", src_ip="", repo="r", findings=0,
                                  detail="", now=0.0)
        self.assertNotIn("commits", event.payload or {})

    def test_build_event_truncates_huge_lists(self):
        with patch.object(audit, "COMMIT_LIST_LIMIT", 2):
            event = audit.build_event("forge.push_accepted", "success", "refs/heads/x",
                                      bottle="b", src_ip="", repo="r", findings=0,
                                      detail="", now=0.0, commits=["1", "2", "3"])
        payload = event.payload or {}
        self.assertEqual((["1", "2"], 3, True), (payload["commits"], payload["commit_count"],
                                                 payload["commits_truncated"]))

    def test_deleted_ref_lists_no_commits(self):
        self.assertEqual([], audit.pushed_commits("0" * 40))


class TestTrailerCheckInProcess(_GitCase):
    """trailer_check's functions called directly against a real bare gate
    whose incoming commits sit in its object store (fetched into
    FETCH_HEAD, which no ref names), as pre-receive sees them."""

    def setUp(self) -> None:
        super().setUp()
        self.gate = self.root / "gate.git"
        self.git("init", "-q", "--bare", str(self.gate), cwd=self.root)
        self.work = self.root / "work"
        self.git("init", "-q", "-b", "main", str(self.work), cwd=self.root)
        self.git("commit", "-q", "--allow-empty", "-m", "upstream", cwd=self.work)
        self.git("push", "-q", str(self.gate), "HEAD:refs/heads/main", cwd=self.work)
        self.upstream = self.head()
        self.baseline_file = self.root / "baseline"
        self.baseline_file.write_text("", encoding="utf-8")
        for key, value in (("git-gate.trailers", "require"), ("git-gate.trailer", VALUE),
                           ("git-gate.baseline", str(self.baseline_file))):
            self.git("config", key, value, cwd=self.gate)
        self.errors: list[str] = []

    def head(self) -> str:
        return self.git("rev-parse", "HEAD", cwd=self.work).stdout.strip()

    def gate_git(self, *args: str, stdin: str = "") -> str:
        return subprocess.run(["git", "-C", str(self.gate), *args], input=stdin,
                              env=self.env, capture_output=True, text=True,
                              check=True).stdout

    def commit(self, msg: str, *trailers: str) -> str:
        args = [a for t in trailers for a in ("--trailer", t)]
        self.git("commit", "-q", "--allow-empty", "-m", msg, *args, cwd=self.work)
        return self.head()

    def incoming(self) -> str:
        """Bring the work repo's HEAD into the gate's objects, no ref."""
        self.git("fetch", "-q", str(self.work), "main", cwd=self.gate)
        return self.head()

    def run_check(self, new: str, old: str = "") -> int:
        return trailer_check.check([(old or self.upstream, new, "refs/heads/main")],
                                   git=self.gate_git, err=self.errors.append)

    def test_accepts_trailered_commits(self):
        self.commit("feat: ok", f"{TRAILER_KEY}: {VALUE}")
        self.assertEqual(trailer_check.ACCEPT, self.run_check(self.incoming()))
        self.assertEqual([], self.errors)

    def test_refuses_and_explains(self):
        self.commit("feat: bare")
        bad = self.commit("feat: other", f"{TRAILER_KEY}: x/y")
        self.assertEqual(trailer_check.REFUSE, self.run_check(self.incoming()))
        text = "\n".join(self.errors)
        self.assertIn("refusing refs/heads/main: 2 new commit(s)", text)
        self.assertIn(f"{bad[:12]} feat: other  (names x/y)", text)
        self.assertIn("feat: bare  (no Bot-Bottle trailer)", text)
        self.assertIn("--exec 'git -c trailer.ifexists=replace commit", text)
        self.assertIn(f" {self.upstream[:12]}", text)

    def test_lists_at_most_ten(self):
        for i in range(12):
            self.commit(f"feat: {i}")
        self.run_check(self.incoming())
        self.assertIn("git-gate:   ... and 2 more", self.errors)

    def test_warn_accepts_and_off_skips(self):
        self.commit("feat: bare")
        new = self.incoming()
        self.git("config", "git-gate.trailers", "warn", cwd=self.gate)
        self.assertEqual(trailer_check.ACCEPT, self.run_check(new))
        self.assertIn("warning refs/heads/main", self.errors[0])
        self.errors.clear()
        self.git("config", "git-gate.trailers", "off", cwd=self.gate)
        self.assertEqual(trailer_check.ACCEPT, self.run_check(new))
        self.assertEqual([], self.errors)

    def test_baseline_commits_are_exempt(self):
        operator = self.commit("feat: operator")
        self.baseline_file.write_text(f"{operator}\n", encoding="utf-8")
        self.commit("feat: agent", f"{TRAILER_KEY}: {VALUE}")
        self.assertEqual(trailer_check.ACCEPT, self.run_check(self.incoming()))
        bare = self.commit("feat: agent bare")
        self.assertEqual(trailer_check.REFUSE, self.run_check(self.incoming()))
        text = "\n".join(self.errors)
        self.assertIn(bare[:12], text)
        self.assertNotIn("feat: operator", text)
        self.assertIn(f" {operator[:12]}", text)

    def test_deletions_and_root_commits(self):
        self.assertEqual(trailer_check.ACCEPT, self.run_check("0" * 40))
        self.assertEqual("--root", trailer_check.rebase_base(self.gate_git, self.upstream))

    def test_rebase_base_placeholders(self):
        self.git("checkout", "-q", "-b", "side", cwd=self.work)
        self.commit("feat: side")
        self.git("push", "-q", str(self.gate), "HEAD:refs/heads/side", cwd=self.work)
        self.git("checkout", "-q", "main", cwd=self.work)
        self.git("merge", "-q", "--no-ff", "-m", "merge", "side", cwd=self.work)
        excluded = trailer_check.exclusions(self.gate_git, [])
        new = self.incoming()
        self.assertEqual("<upstream branch, e.g. origin/main>",
                         trailer_check.rebase_base(self.gate_git, new, excluded))

        def failing(*args: str, stdin: str = "") -> str:
            raise subprocess.CalledProcessError(128, ["git", *args], stderr="bad")
        self.assertEqual("<upstream branch>", trailer_check.rebase_base(failing, new))

    def test_offenders_defaults_to_the_gates_refs(self):
        self.commit("feat: bare")
        found = trailer_check.offenders(self.gate_git, self.incoming(), VALUE)
        self.assertEqual(["feat: bare"], [o.subject for o in found])

    def test_git_failures_refuse(self):
        def refs_fail(*args: str, stdin: str = "") -> str:
            if args[0] == "for-each-ref":
                raise subprocess.CalledProcessError(1, "git", stderr="broken refs")
            return self.gate_git(*args, stdin=stdin)
        self.assertEqual(trailer_check.MISCONFIGURED, trailer_check.check(
            [(self.upstream, self.upstream, "refs/heads/main")], git=refs_fail,
            err=self.errors.append))
        self.assertIn("could not list the gate's refs: broken refs", self.errors[-1])
        def log_fails(*args: str, stdin: str = "") -> str:
            if args[0] == "log":
                raise subprocess.CalledProcessError(128, "git", stderr="bad object")
            return self.gate_git(*args, stdin=stdin)
        self.assertEqual(trailer_check.MISCONFIGURED, trailer_check.check(
            [(self.upstream, "f" * 40, "refs/heads/main")], git=log_fails,
            err=self.errors.append))
        self.assertIn("could not read the commits pushed to refs/heads/main", self.errors[-1])

    def test_main_reads_the_refs_file_in_the_repo(self):
        self.commit("feat: bare")
        new = self.incoming()
        refs = self.root / "refs"
        refs.write_text(f"{self.upstream} {new} refs/heads/main\n", encoding="utf-8")
        cwd = os.getcwd()
        stderr = io.StringIO()
        try:
            os.chdir(self.gate)
            with patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1",
                                         "GIT_CONFIG_GLOBAL": os.devnull}), \
                    redirect_stderr(stderr):
                rc = trailer_check.main(["trailer_check", str(refs)])
                usage = trailer_check.main(["trailer_check"])
        finally:
            os.chdir(cwd)
        self.assertEqual((trailer_check.REFUSE, trailer_check.REFUSE), (rc, usage))
        self.assertIn("feat: bare", stderr.getvalue())
        self.assertIn("usage: trailer_check <refs-file>", stderr.getvalue())


    def test_audit_lists_pushed_commits_in_process(self):
        self.git("config", "git-gate.trailer", VALUE, cwd=self.gate)
        first = self.commit("feat: one")
        second = self.commit("feat: two")
        new = self.incoming()
        cwd = os.getcwd()
        try:
            os.chdir(self.gate)
            with patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1",
                                         "GIT_CONFIG_GLOBAL": os.devnull}):
                commits = audit.pushed_commits(new)
                trailer = audit.trailer_value()
                missing = audit.pushed_commits("e" * 40)
                with patch.dict(os.environ, {"BOT_BOTTLE_ORCHESTRATOR_URL":
                                             "http://127.0.0.1:9"}), \
                        redirect_stderr(io.StringIO()):
                    rc = audit.main(["forge.push_accepted", "success", "refs/heads/main",
                                     "--findings", "0", "--new", new])
        finally:
            os.chdir(cwd)
        self.assertEqual(([first, second], VALUE, None, 0), (commits, trailer, missing, rc))
        self.assertEqual([], audit.pushed_commits(""))


class TestSmallFallbacks(unittest.TestCase):
    def test_hooks_env_with_a_junk_count_starts_at_zero(self):
        env = commit_hooks.hooks_env("/home/node", {"GIT_CONFIG_COUNT": "lots"})
        self.assertEqual("1", env["GIT_CONFIG_COUNT"])
        self.assertEqual("core.hooksPath", env["GIT_CONFIG_KEY_0"])

    def test_workspace_baseline_empty_when_git_cannot_run(self):
        def run(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("git")
        self.assertEqual([], workspace_baseline(Path("/x"), run=run))


class TestTrailerCheckUnits(unittest.TestCase):
    def test_unknown_mode_refuses(self):
        def git(*args: str) -> str:
            return "bogus\n" if args[-1] == "git-gate.trailers" else ""
        errors: list[str] = []
        self.assertEqual(trailer_check.MISCONFIGURED, trailer_check.check([], git=git, err=errors.append))
        self.assertIn("git-gate.trailers is missing or invalid", errors[0])

    def test_require_without_value_refuses(self):
        def git(*args: str) -> str:
            if args[-1] == "git-gate.trailers":
                return "require\n"
            raise subprocess.CalledProcessError(1, "git")
        self.assertEqual(trailer_check.MISCONFIGURED, trailer_check.check([], git=git, err=lambda _s: None))

    def test_read_refs_skips_blank_lines(self):
        with tempfile.NamedTemporaryFile("w", suffix=".refs", delete=False) as f:
            f.write(f"{'0' * 40} {'1' * 40} refs/heads/main\n\n")
        try:
            self.assertEqual([("0" * 40, "1" * 40, "refs/heads/main")],
                             trailer_check.read_refs(Path(f.name)))
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
