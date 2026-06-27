"""Unit: GitGate prepare shape + entrypoint/hook render (PRD 0008)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.git_gate import (
    GitGate,
    GitGatePlan,
    GitGateUpstream,
    git_gate_known_hosts_line,
    git_gate_render_access_hook,
    git_gate_render_entrypoint,
    git_gate_render_hook,
    revoke_git_gate_provisioned_keys,
    _resolve_identity_file,
    git_gate_upstreams_for_bottle,
)
from bot_bottle.manifest import ManifestIndex
from tests.fixtures import fixture_minimal, fixture_with_git


class _StubGate(GitGate):
    def start(self, plan: GitGatePlan) -> str:
        raise NotImplementedError

    def stop(self, target: str) -> None:
        raise NotImplementedError


class TestUpstreamsForBottle(unittest.TestCase):
    def test_one_upstream_per_git_entry(self):
        bottle = fixture_with_git().bottles["dev"]
        ups = git_gate_upstreams_for_bottle(bottle)
        self.assertEqual(2, len(ups))
        self.assertEqual("bot-bottle", ups[0].name)
        self.assertEqual("gitea.dideric.is", ups[0].upstream_host)
        self.assertEqual("30009", ups[0].upstream_port)
        self.assertEqual("foo", ups[1].name)
        self.assertEqual("github.com", ups[1].upstream_host)
        self.assertEqual("22", ups[1].upstream_port)

    def test_empty_bottle_yields_empty_upstreams(self):
        bottle = fixture_minimal().bottles["dev"]
        self.assertEqual((), git_gate_upstreams_for_bottle(bottle))


class TestKnownHostsLine(unittest.TestCase):
    def test_default_port_unbracketed(self):
        line = git_gate_known_hosts_line("github.com", "22", "ssh-ed25519 AAAA")
        self.assertEqual("github.com ssh-ed25519 AAAA\n", line)

    def test_non_default_port_bracketed(self):
        line = git_gate_known_hosts_line("gitea.dideric.is", "30009", "ssh-ed25519 AAAA")
        self.assertEqual("[gitea.dideric.is]:30009 ssh-ed25519 AAAA\n", line)


class TestEntrypointRender(unittest.TestCase):
    def test_one_init_repo_call_per_upstream(self):
        ups = (
            GitGateUpstream(
                name="bot-bottle",
                upstream_url="ssh://git@gitea.dideric.is:30009/didericis/bot-bottle.git",
                upstream_host="gitea.dideric.is",
                upstream_port="30009",
                identity_file="/host/path/key",
                known_host_key="ssh-ed25519 AAAA",
            ),
            GitGateUpstream(
                name="foo",
                upstream_url="ssh://git@github.com/didericis/foo.git",
                upstream_host="github.com",
                upstream_port="22",
                identity_file="/host/path/key2",
                known_host_key="",
            ),
        )
        script = git_gate_render_entrypoint(ups)
        self.assertIn("#!/bin/sh", script)
        # shlex.quote leaves safe strings unquoted; verify via token parse.
        import shlex as _shlex
        lines_with_init = [l for l in script.splitlines() if l.startswith("init_repo ")]
        self.assertEqual(2, len(lines_with_init))
        self.assertEqual(
            ["init_repo", "bot-bottle",
             "ssh://git@gitea.dideric.is:30009/didericis/bot-bottle.git"],
            _shlex.split(lines_with_init[0]),
        )
        self.assertEqual(
            ["init_repo", "foo", "ssh://git@github.com/didericis/foo.git"],
            _shlex.split(lines_with_init[1]),
        )
        # Daemon line is what keeps PID 1 alive.
        self.assertIn("exec git daemon", script)
        self.assertIn("--enable=receive-pack", script)
        self.assertIn("--timeout=15", script)
        self.assertIn("--init-timeout=15", script)
        self.assertIn("--base-path=/git", script)
        # Smart HTTP receive-pack uses the same bare repos and hooks
        # as git-daemon, so repos must opt in to HTTP pushes too.
        self.assertIn("http.receivepack true", script)
        # The gate must advertise push-option support so clients can
        # pass forge-specific options through to the pre-receive hook.
        self.assertIn("receive.advertisePushOptions true", script)
        # The access-hook is what makes fetch a mirror operation
        # against the upstream (PRD 0008 v1.1).
        self.assertIn("--access-hook=/etc/git-gate/access-hook", script)
        # Each repo's `origin` remote is wired to the upstream via
        # --mirror=fetch so `git fetch origin` mirrors all refs.
        self.assertIn("remote add --mirror=fetch origin", script)

    def test_empty_upstreams_still_execs_daemon(self):
        # A no-upstream gate is a no-op for repos but the daemon still
        # has to start so the entrypoint doesn't exit.
        script = git_gate_render_entrypoint(())
        self.assertNotIn("init_repo '", script)
        self.assertIn("exec git daemon", script)

    def test_single_quote_in_upstream_url_is_escaped(self):
        ups = (GitGateUpstream(
            name="myrepo",
            upstream_url="ssh://git@host/path'with'quotes.git",
            upstream_host="host",
            upstream_port="22",
            identity_file="/key",
            known_host_key="",
        ),)
        script = git_gate_render_entrypoint(ups)
        self.assertNotIn(
            "init_repo 'myrepo' 'ssh://git@host/path'with'quotes.git'",
            script,
        )
        self.assertIn("init_repo", script)
        self.assertIn("path", script)

    def test_space_and_semicolon_in_upstream_url_are_escaped(self):
        import shlex as _shlex
        raw_url = "ssh://git@host/path with spaces;evil.git"
        ups = (GitGateUpstream(
            name="myrepo",
            upstream_url=raw_url,
            upstream_host="host",
            upstream_port="22",
            identity_file="/key",
            known_host_key="",
        ),)
        script = git_gate_render_entrypoint(ups)
        line = next(l for l in script.splitlines() if l.startswith("init_repo "))
        tokens = _shlex.split(line)
        self.assertEqual(3, len(tokens))
        self.assertEqual("myrepo", tokens[1])
        self.assertEqual(raw_url, tokens[2])


class TestHookRender(unittest.TestCase):
    def test_pre_receive_hook_has_two_phases(self):
        hook = git_gate_render_hook()
        # Phase 1: gitleaks. Phase 2: forward to origin.
        self.assertIn("gitleaks git", hook)
        self.assertIn("git push", hook)
        self.assertIn("origin \"$refspec\"", hook)
        # KnownHostKey absence is fail-closed.
        self.assertIn("refusing to push", hook)
        # Stdin is buffered to a tempfile so both phases can re-read.
        self.assertIn("refs_file=$(mktemp)", hook)

    def test_new_ref_scan_scoped_to_incoming_commits(self):
        # A new branch (old=all-zeros) must scan only commits new to the
        # gate, not the full ancestry — otherwise historical findings
        # block every new-branch push (PRD 0028 / issue #106).
        hook = git_gate_render_hook()
        self.assertIn('log_opts="$new --not --all"', hook)
        # The old over-broad full-ancestry range must be gone.
        self.assertNotIn('log_opts="$new"', hook)
        # Existing-branch delta scan is unchanged.
        self.assertIn('log_opts="$old..$new"', hook)

    def test_forward_ssh_is_non_interactive_and_bounded(self):
        # No prompt (BatchMode) and a connect timeout, so an unreachable
        # upstream fails fast instead of hanging the receive-pack.
        hook = git_gate_render_hook()
        self.assertIn("BatchMode=yes", hook)
        self.assertIn("ConnectTimeout=", hook)

    def test_force_push_uses_plus_refspec(self):
        # A non-fast-forward push (old != zero, new not a descendant of old)
        # must forward +$new:$ref so the upstream accepts the force push.
        hook = git_gate_render_hook()
        self.assertIn('git merge-base --is-ancestor "$old" "$new"', hook)
        self.assertIn('refspec="+$new:$ref"', hook)

    def test_forward_preserves_push_options(self):
        # Git exposes push options to pre-receive hooks as
        # GIT_PUSH_OPTION_COUNT + indexed GIT_PUSH_OPTION_N variables.
        # Forward them as first-class argv entries so spaces and shell
        # metacharacters inside option values remain data.
        hook = git_gate_render_hook()
        self.assertIn("push_option_count=${GIT_PUSH_OPTION_COUNT:-0}", hook)
        self.assertIn('opt=$(printenv "GIT_PUSH_OPTION_$i" || :)', hook)
        self.assertIn('set -- "$@" --push-option="$opt"', hook)
        self.assertIn('git push "$@" origin "$refspec"', hook)

    def test_inline_gitleaks_allow_routes_to_supervisor(self):
        hook = git_gate_render_hook()
        # First gitleaks runs normally; only if that passes does the
        # hook ask gitleaks to ignore inline allow comments and report
        # the suppressed findings for human approval.
        self.assertIn("--ignore-gitleaks-allow", hook)
        self.assertIn("--report-format=json", hook)
        self.assertIn('"tool": "gitleaks-allow"', hook)
        self.assertIn("SUPERVISE_QUEUE_DIR", hook)
        self.assertIn("SUPERVISE_BOTTLE_SLUG", hook)
        self.assertIn("supervisor approved # gitleaks:allow", hook)
        self.assertIn("supervisor rejected # gitleaks:allow", hook)

    def test_inline_gitleaks_allow_fails_closed_without_supervisor(self):
        hook = git_gate_render_hook()
        self.assertIn(
            "cannot route # gitleaks:allow finding to supervisor; refusing push",
            hook,
        )
        self.assertIn(
            "supervisor approval timed out for # gitleaks:allow; refusing push",
            hook,
        )


class TestAccessHookRender(unittest.TestCase):
    def test_access_hook_refreshes_origin_on_upload_pack(self):
        hook = git_gate_render_access_hook()
        # Service-name guard: only upload-pack (fetch / clone / pull /
        # ls-remote) triggers the upstream refresh; receive-pack
        # bypasses this and the pre-receive hook gates it instead.
        self.assertIn('service=$1', hook)
        self.assertIn('"$service" != "upload-pack"', hook)
        # The fetch is what makes the gate a transparent mirror.
        self.assertIn("git -C \"$repo_dir\" fetch origin --prune", hook)

    def test_access_hook_fail_closed_on_upstream_error(self):
        hook = git_gate_render_access_hook()
        # Upstream-fetch failure exits non-zero, which propagates to
        # the agent's fetch as a real error rather than stale data.
        self.assertIn("refusing to serve stale data", hook)
        self.assertIn("exit 1", hook)

    def test_access_hook_ssh_is_non_interactive_and_bounded(self):
        # Same hardening as the forward path: the fetch ssh must not
        # prompt and must time out rather than hang upload-pack.
        hook = git_gate_render_access_hook()
        self.assertIn("BatchMode=yes", hook)
        self.assertIn("ConnectTimeout=", hook)


class TestPrepare(unittest.TestCase):
    def setUp(self):
        self.stage = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil

        shutil.rmtree(self.stage, ignore_errors=True)

    def test_prepare_writes_all_three_scripts(self):
        plan = _StubGate().prepare(
            fixture_with_git().bottles["dev"], "demo", self.stage
        )
        self.assertEqual(
            self.stage / "git_gate_entrypoint.sh", plan.entrypoint_script
        )
        self.assertEqual(
            self.stage / "git_gate_pre_receive.sh", plan.hook_script
        )
        self.assertEqual(
            self.stage / "git_gate_access_hook.sh", plan.access_hook_script
        )
        # Entrypoint + pre-receive are mode 600 (loaded into the
        # gate by docker cp and then `install -m 755`'d into each
        # bare repo's hooks/ — source bit doesn't matter). The
        # access-hook is execed directly by git daemon, so it has to
        # carry the x bit through docker cp.
        self.assertEqual(0o600, os.stat(plan.entrypoint_script).st_mode & 0o777)
        self.assertEqual(0o600, os.stat(plan.hook_script).st_mode & 0o777)
        self.assertEqual(0o700, os.stat(plan.access_hook_script).st_mode & 0o777)

    def test_prepare_plan_carries_upstreams_and_slug(self):
        plan = _StubGate().prepare(
            fixture_with_git().bottles["dev"], "demo", self.stage
        )
        self.assertEqual("demo", plan.slug)
        self.assertEqual(2, len(plan.upstreams))
        self.assertEqual("", plan.internal_network)
        self.assertEqual("", plan.egress_network)

    def test_prepare_writes_known_hosts_file(self):
        plan = _StubGate().prepare(
            fixture_with_git().bottles["dev"], "demo", self.stage
        )
        upstream = plan.upstreams[0]
        self.assertEqual(self.stage / "bot-bottle-known_hosts",
                         upstream.known_hosts_file)
        self.assertEqual(
            "[gitea.dideric.is]:30009 ssh-ed25519 AAAA...\n",
            upstream.known_hosts_file.read_text(),
        )
        self.assertEqual(0o600, os.stat(upstream.known_hosts_file).st_mode & 0o777)

    def test_prepare_skips_known_hosts_file_when_key_missing(self):
        manifest = ManifestIndex.from_json_obj({
            "bottles": {"dev": {"git-gate": {"repos": {
                "foo": {
                    "url": "ssh://git@github.com/didericis/foo.git",
                    "key": {"provider": "static", "path": "/dev/null"},
                },
            }}}},
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })
        plan = _StubGate().prepare(
            manifest.bottles["dev"], "demo", self.stage
        )
        self.assertEqual(Path(), plan.upstreams[0].known_hosts_file)

    def test_prepare_with_no_git_writes_minimal_script(self):
        plan = _StubGate().prepare(
            fixture_minimal().bottles["dev"], "demo", self.stage
        )
        self.assertEqual((), plan.upstreams)
        content = plan.entrypoint_script.read_text()
        self.assertNotIn("init_repo '", content)
        self.assertIn("exec git daemon", content)


class TestDynamicKeyProvisioning(unittest.TestCase):
    def setUp(self):
        self.stage = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil

        shutil.rmtree(self.stage, ignore_errors=True)

    def _gitea_manifest(self):
        return ManifestIndex.from_json_obj({
            "bottles": {
                "dev": {
                    "git-gate": {
                        "repos": {
                            "repo": {
                                "url": "ssh://git@gitea.example.com/org/repo.git",
                                "key": {
                                    "provider": "gitea",
                                    "forge_token_env": "GITEA_TOKEN",
                                },
                                "host_key": "ssh-ed25519 AAAA...",
                            },
                        },
                    }
                }
            },
            "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
        })

    def test_resolve_identity_file_static_uses_entry_path(self):
        entry = fixture_with_git().bottles["dev"].git[0]
        self.assertEqual(entry.IdentityFile, _resolve_identity_file(entry, "demo", self.stage))

    def test_resolve_identity_file_gitea_provisions_key(self):
        entry = self._gitea_manifest().bottles["dev"].git[0]
        with patch("bot_bottle.git_gate_provision._provision_dynamic_key", return_value="/tmp/provisioned-key") as mock_provision:
            self.assertEqual("/tmp/provisioned-key", _resolve_identity_file(entry, "demo", self.stage))
        mock_provision.assert_called_once()

    def test_revoke_skips_non_gitea_and_missing_id_file(self):
        revoke_git_gate_provisioned_keys(fixture_with_git().bottles["dev"], self.stage)

    def test_revoke_calls_delete_for_gitea_entry(self):
        bottle = self._gitea_manifest().bottles["dev"]
        (self.stage / "repo-deploy-key-id").write_text("123\n")
        with patch.dict("os.environ", {"GITEA_TOKEN": "token"}), patch(
            "bot_bottle.deploy_key_provisioner.get_provisioner"
        ) as mock_get_provisioner:
            provisioner = mock_get_provisioner.return_value
            revoke_git_gate_provisioned_keys(bottle, self.stage)
        mock_get_provisioner.assert_called_once()
        provisioner.delete.assert_called_once_with("org/repo", "123")

    def test_revoke_missing_token_raises(self):
        bottle = self._gitea_manifest().bottles["dev"]
        (self.stage / "repo-deploy-key-id").write_text("123\n")
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(RuntimeError) as cm:
            revoke_git_gate_provisioned_keys(bottle, self.stage)
        self.assertIn("env var is not set", str(cm.exception))


class TestShellEscaping(unittest.TestCase):
    """Regression tests: all three render functions must produce syntactically
    valid sh code even when names and upstream URLs contain shell-special
    characters.  Tests construct GitGateUpstream directly — bypassing manifest
    name validation — so the rendering layer is exercised in isolation."""

    _MALICIOUS_URL_CASES = [
        ("single_quote", "ssh://git@host/path'with'quotes.git"),
        ("double_quote", 'ssh://git@host/path"with"quotes.git'),
        ("space",        "ssh://git@host/path with spaces.git"),
        ("semicolon",    "ssh://git@host/path;evil.git"),
        ("newline",      "ssh://git@host/path\nwith\nnewlines.git"),
        ("backtick",     "ssh://git@host/path`whoami`.git"),
    ]

    _MALICIOUS_NAME_CASES = [
        ("single_quote", "repo'name"),
        ("double_quote", 'repo"name'),
        ("space",        "repo name"),
        ("semicolon",    "repo;name"),
        ("newline",      "repo\nname"),
        ("backtick",     "repo`name"),
    ]

    def _make_upstream(self, url: str, name: str = "myrepo") -> GitGateUpstream:
        return GitGateUpstream(
            name=name,
            upstream_url=url,
            upstream_host="host",
            upstream_port="22",
            identity_file="/key",
            known_host_key="",
        )

    def _assert_valid_sh(self, script: str, label: str = "") -> None:
        import subprocess
        fd, path = tempfile.mkstemp(suffix=".sh")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(script)
            result = subprocess.run(
                ["sh", "-n", path], capture_output=True, text=True,
            )
            self.assertEqual(
                0, result.returncode,
                f"sh -n failed{(' for ' + label) if label else ''}: {result.stderr}",
            )
        finally:
            os.unlink(path)

    def test_hook_renders_valid_sh(self):
        self._assert_valid_sh(git_gate_render_hook(), "pre-receive hook")

    def test_access_hook_renders_valid_sh(self):
        self._assert_valid_sh(git_gate_render_access_hook(), "access hook")

    def test_entrypoint_with_pathological_upstream_url_renders_valid_sh(self):
        for label, url in self._MALICIOUS_URL_CASES:
            with self.subTest(char=label):
                script = git_gate_render_entrypoint((self._make_upstream(url),))
                self._assert_valid_sh(script, label)

    def test_entrypoint_upstream_url_value_preserved_after_quoting(self):
        import shlex as _shlex
        for label, url in self._MALICIOUS_URL_CASES:
            with self.subTest(char=label):
                script = git_gate_render_entrypoint((self._make_upstream(url),))
                # The quoted form of the URL must appear verbatim in the script so
                # the shell reconstructs exactly the original value at runtime.
                expected = f"init_repo {_shlex.quote('myrepo')} {_shlex.quote(url)}"
                self.assertIn(
                    expected, script,
                    f"{label}: expected quoted form not found in script",
                )

    def test_entrypoint_with_pathological_name_renders_valid_sh(self):
        for label, name in self._MALICIOUS_NAME_CASES:
            with self.subTest(char=label):
                script = git_gate_render_entrypoint((
                    self._make_upstream("ssh://git@github.com/foo/bar.git", name=name),
                ))
                self._assert_valid_sh(script, label)

    def test_entrypoint_name_value_preserved_after_quoting(self):
        import shlex as _shlex
        url = "ssh://git@github.com/foo/bar.git"
        for label, name in self._MALICIOUS_NAME_CASES:
            with self.subTest(char=label):
                script = git_gate_render_entrypoint((
                    self._make_upstream(url, name=name),
                ))
                expected = f"init_repo {_shlex.quote(name)} {_shlex.quote(url)}"
                self.assertIn(
                    expected, script,
                    f"{label}: expected quoted form not found in script",
                )


if __name__ == "__main__":
    unittest.main()
