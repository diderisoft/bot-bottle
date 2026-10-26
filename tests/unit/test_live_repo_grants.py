"""Granting a repo to a running bottle (PRD 0105): the live grant, keeping it
in a bottle, and the phone's card and answer for `add-repo`."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from bot_bottle.approvals import DENY, ALLOW_ONCE, KeepTarget, Request, answer, keep_grant
from bot_bottle.bottle_state import BottleMetadata, git_gate_state_dir
from bot_bottle.contract.yaml_subset import parse_frontmatter
from bot_bottle.control.client import AlreadyAnswered
from bot_bottle.control.git_gate_render import git_gate_render_gitconfig
from bot_bottle.control.hostctl import remote_actions, repo_grants
from bot_bottle.git_gate import live_grant
from bot_bottle.git_gate.provision import live_grant_refs, load_upstreams_snapshot
from bot_bottle.manifest.forges import ManifestForge

HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
FORGE = ManifestForge(name="gitea.example.com", kind="gitea", url="https://gitea.example.com",
                      ssh="ssh://git@gitea.example.com:2222", token_secret="GITEA_TOKEN",
                      host_key=HOST_KEY)
FORGE_BOTTLE = f"""---
credentials:
  gitea-api:
    source: secret
    secret: GITEA_TOKEN
git-gate:
  forges:
    gitea.example.com:
      kind: gitea
      url: https://gitea.example.com
      ssh: ssh://git@gitea.example.com:2222
      token: $gitea-api
      host_key: {HOST_KEY}
---

The forge.
"""
ARRIVED = "2026-10-26T10:00:00+00:00"


class _Root(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "root"
        self.bottles = self.root / "bottles"
        self.bottles.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"BOT_BOTTLE_ROOT": str(self.root)})
        env.start()
        self.addCleanup(env.stop)


class _Guest:
    """A running bottle's exec, recording each script."""

    def __init__(self, header: str = "http.http://10.0.0.2:9420/.extraheader x-id: t\n",
                 add_code: int = 0, proxy: str = "", rewrites: str = "") -> None:
        self.scripts: list[str] = []
        self.header, self.add_code, self.proxy = header, add_code, proxy
        self.rewrites = rewrites

    def exec(self, script: str, *, user: str = "node") -> Any:
        del user
        self.scripts.append(script)
        if "--get-regexp" in script:
            return SimpleNamespace(returncode=0 if self.header else 1, stdout=self.header,
                                   stderr="")
        if "--get-all" in script:
            return SimpleNamespace(returncode=0 if self.rewrites else 1,
                                   stdout=self.rewrites, stderr="")
        if "/proc/" in script:
            return SimpleNamespace(returncode=0 if self.proxy else 1, stdout=self.proxy,
                                   stderr="")
        return SimpleNamespace(returncode=self.add_code, stdout="", stderr="locked")


class TestLiveGrant(_Root):
    def test_grant_entry_expands_like_a_bottle_grant(self) -> None:
        entry = live_grant.grant_entry("gitea.example.com/acme/app:write", FORGE)
        self.assertEqual(("app", "ssh://git@gitea.example.com:2222/acme/app.git", False),
                         (entry.Name, entry.Upstream, entry.Key.read_only))
        self.assertEqual(("GITEA_TOKEN", HOST_KEY), (entry.Key.forge_token_env,
                                                     entry.KnownHostKey))
        self.assertTrue(live_grant.grant_entry("gitea.example.com/acme/lib", FORGE).Key.read_only)

    def test_provision_records_the_key_and_adds_to_the_snapshot(self) -> None:
        stage = git_gate_state_dir("s1")
        entry = live_grant.grant_entry("gitea.example.com/acme/app", FORGE)

        def mint(e: Any, slug: str, stage_dir: Path, secrets: Any) -> str:
            del e, secrets
            self.assertEqual(1, len(live_grant_refs(stage_dir)))  # recorded first
            (stage_dir / "app-deploy-key-id").write_text("7")
            return str(stage_dir / f"{slug}-key")

        with mock.patch.object(live_grant, "_provision_dynamic_key", side_effect=mint):
            upstream = live_grant.provision_live(entry, slug="s1", stage_dir=stage,
                                                 secrets=mock.Mock())
        self.assertFalse(upstream.writable)
        (snap,) = load_upstreams_snapshot(stage)
        self.assertEqual(("app", str(stage / "s1-key")), (snap.name, snap.identity_file))
        self.assertIn("[gitea.example.com]:2222 ssh-ed25519",
                      (stage / "app-known_hosts").read_text())
        self.assertEqual("acme/app.git", live_grant_refs(stage)[0].upstream_path)
        with self.assertRaisesRegex(live_grant.GrantError, r"already has app \(read-only\)"):
            live_grant.provision_live(entry, slug="s1", stage_dir=stage, secrets=mock.Mock())
        other = live_grant.grant_entry("gitea.example.com/elsewhere/app", FORGE)
        with self.assertRaisesRegex(live_grant.GrantError, "different repo named 'app'"):
            live_grant.provision_live(other, slug="s1", stage_dir=stage, secrets=mock.Mock())

    def test_regranting_with_write_replaces_the_key(self) -> None:
        stage = git_gate_state_dir("s1")
        keys = iter(["7", "8"])

        def mint(e: Any, slug: str, stage_dir: Path, secrets: Any) -> str:
            del e, slug, secrets
            (stage_dir / "app-deploy-key-id").write_text(next(keys))
            return str(stage_dir / "app-key")

        with mock.patch.object(live_grant, "_provision_dynamic_key", side_effect=mint), \
                mock.patch.object(live_grant, "revoke_key") as revoke:
            live_grant.provision_live(live_grant.grant_entry("gitea.example.com/acme/app", FORGE),
                                      slug="s1", stage_dir=stage, secrets=mock.Mock())
            revoke.assert_not_called()
            live_grant.provision_live(
                live_grant.grant_entry("gitea.example.com/acme/app:write", FORGE),
                slug="s1", stage_dir=stage, secrets=mock.Mock())
        self.assertEqual("7", revoke.call_args.args[1])  # the old key, after minting
        (snap,) = load_upstreams_snapshot(stage)
        self.assertTrue(snap.writable)
        self.assertEqual(1, len(live_grant_refs(stage)))

    def test_revoking_once_per_repo(self) -> None:
        from bot_bottle.git_gate.provision import (  # pylint: disable=import-outside-toplevel
            DeployKeyRef, revoke_deploy_keys)
        stage = git_gate_state_dir("s1")
        stage.mkdir(parents=True)
        (stage / "app-deploy-key-id").write_text("8")
        ref = DeployKeyRef("app", "GITEA_TOKEN", "", "gitea.example.com", "acme/app.git")
        provisioner = mock.Mock()
        with mock.patch("bot_bottle.deploy_key_provisioner.get_provisioner",
                        return_value=provisioner):
            revoke_deploy_keys((ref, ref), stage, mock.Mock(get=lambda _n: "tok"))
        provisioner.delete.assert_called_once_with("acme/app", "8")

    def test_needs_a_pinned_host_key(self) -> None:
        from dataclasses import replace  # pylint: disable=import-outside-toplevel
        entry = live_grant.grant_entry("gitea.example.com/acme/app", replace(FORGE, host_key=""))
        with self.assertRaisesRegex(live_grant.GrantError, "no SSH host key"):
            live_grant.provision_live(entry, slug="s1", stage_dir=git_gate_state_dir("s1"),
                                      secrets=mock.Mock())

    def test_only_forge_grants(self) -> None:
        from dataclasses import replace  # pylint: disable=import-outside-toplevel
        entry = live_grant.grant_entry("gitea.example.com/acme/app", FORGE)
        entry = replace(entry, Key=replace(entry.Key, provider="static"))
        with self.assertRaisesRegex(live_grant.GrantError, "only forge"):
            live_grant.provision_live(entry, slug="s1", stage_dir=git_gate_state_dir("s1"),
                                      secrets=mock.Mock())

    def test_teardown_revokes_live_grants_too(self) -> None:
        from bot_bottle.git_gate.provision import (  # pylint: disable=import-outside-toplevel
            DeployKeyRef, record_live_grant, revoke_git_gate_provisioned_keys)
        from bot_bottle.manifest import ManifestBottle  # pylint: disable=import-outside-toplevel
        stage = git_gate_state_dir("s1")
        stage.mkdir(parents=True)
        ref = DeployKeyRef("app", "GITEA_TOKEN", "https://gitea.example.com",
                           "gitea.example.com", "acme/app.git")
        record_live_grant(stage, ref)
        with mock.patch("bot_bottle.git_gate.provision.revoke_deploy_keys") as revoke:
            revoke_git_gate_provisioned_keys(ManifestBottle(), stage, mock.Mock())
        self.assertEqual((ref,), revoke.call_args.args[0])

    def test_point_agent_at_gate(self) -> None:
        entry = live_grant.grant_entry("gitea.example.com/acme/app", FORGE)
        guest = _Guest()
        live_grant.point_agent_at_gate(guest, entry)  # type: ignore[arg-type]
        self.assertEqual("git config --global --add url.http://10.0.0.2:9420/app.git.insteadOf "
                         "ssh://git@gitea.example.com:2222/acme/app.git", guest.scripts[-1])
        with self.assertRaisesRegex(live_grant.GrantError, "relaunch"):
            live_grant.point_agent_at_gate(_Guest(header=""), entry)  # type: ignore[arg-type]
        with self.assertRaisesRegex(live_grant.GrantError, "relaunch"):  # a malformed token
            live_grant.point_agent_at_gate(_Guest(  # type: ignore[arg-type]
                header="", proxy="HTTPS_PROXY=http://bottle:a'b@gw:9099\n"), entry)

    def test_a_bottle_without_the_header_gets_it_from_its_proxy(self) -> None:
        entry = live_grant.grant_entry("gitea.example.com/acme/app", FORGE)
        guest = _Guest(header="", proxy="HTTPS_PROXY=http://bottle:tok-1@bot-bottle-gateway:9099\n")
        live_grant.point_agent_at_gate(guest, entry)  # type: ignore[arg-type]
        self.assertEqual("git config --global http.http://bot-bottle-gateway:9420/.extraHeader "
                         "'x-bot-bottle-identity: tok-1'", guest.scripts[2])
        self.assertIn("--add url.http://bot-bottle-gateway:9420/app.git.insteadOf", guest.scripts[4])

    def test_an_existing_rewrite_is_kept(self) -> None:
        entry = live_grant.grant_entry("gitea.example.com/acme/app", FORGE)
        guest = _Guest(rewrites=f"{entry.Upstream}\n")
        live_grant.point_agent_at_gate(guest, entry)  # type: ignore[arg-type]
        self.assertFalse(any("--add" in s for s in guest.scripts))
        with self.assertRaisesRegex(live_grant.GrantError, "locked"):
            live_grant.point_agent_at_gate(_Guest(add_code=1), entry)  # type: ignore[arg-type]

    def test_gitconfig_keeps_the_header_without_repos(self) -> None:
        out = git_gate_render_gitconfig((), "10.0.0.2:9420", identity_token="tok")
        self.assertIn('[http "http://10.0.0.2:9420/"]', out)
        self.assertNotIn("insteadOf", out)


class TestFindForge(_Root):
    def meta(self, *names: str) -> Any:
        return mock.patch("bot_bottle.bottle_state.read_metadata", return_value=BottleMetadata(
            identity="s1", agent_name="pi", cwd="", copy_cwd=False, started_at="",
            bottle_names=names))

    def setUp(self) -> None:
        super().setUp()
        (self.bottles / "gitea.md").write_text(FORGE_BOTTLE)
        (self.bottles / "work.md").write_text("---\nextends: gitea\n---\n\nWork.\n")
        (self.bottles / "plain.md").write_text("---\nenv:\n  A: b\n---\n\nPlain.\n")
        (self.bottles / "broken.md").write_text("---\nextends: nowhere\n---\n")
        project = mock.patch("bot_bottle.approvals.session_project", return_value=None)
        project.start()
        self.addCleanup(project.stop)

    def test_from_the_launched_chain(self) -> None:
        with self.meta("work"):
            found = repo_grants.find_forge("s1", "gitea.example.com")
        assert found is not None
        self.assertEqual(("GITEA_TOKEN", ""), (found.forge.token_secret, found.bottle))

    def test_else_from_a_host_bottle_that_keeping_would_extend(self) -> None:
        with self.meta("plain"):
            found = repo_grants.find_forge("s1", "gitea.example.com")
            self.assertIsNone(repo_grants.find_forge("s1", "other.example.com"))
        assert found is not None
        self.assertEqual("gitea", found.bottle)


class TestGrantRepo(_Root):
    def test_grants_into_the_running_bottle(self) -> None:
        backend, guest = mock.Mock(), _Guest()
        backend.attach_adopted.return_value = guest
        adopted = SimpleNamespace(registry_id="reg-1")
        with mock.patch.object(repo_grants, "find_forge",
                               return_value=repo_grants.FoundForge(FORGE, "gitea")), \
                mock.patch("bot_bottle.backend.adoption.read", return_value=adopted), \
                mock.patch.object(live_grant, "_provision_dynamic_key", return_value="/k"), \
                mock.patch("bot_bottle.backend.gateway_attach.reprovision_git_gate") as reprov:
            result = repo_grants.grant_repo(backend, "s1", "gitea.example.com/acme/app:write",
                                            secrets_file=self.root / "secrets")
        self.assertEqual({"repo": "app", "url": "ssh://git@gitea.example.com:2222/acme/app.git",
                          "writable": True, "extends": "gitea"}, result)
        self.assertEqual(("reg-1", "s1"), reprov.call_args.args[1:])
        self.assertIn("insteadOf", guest.scripts[-1])

    def test_errors(self) -> None:
        grant = "gitea.example.com/acme/app"
        with mock.patch.object(repo_grants, "find_forge", return_value=None):
            self.assertIn("connect it first", str(repo_grants.grant_repo(
                mock.Mock(), "s1", grant, secrets_file=self.root / "s")["error"]))
        self.assertIn("<forge>/<owner>/<repo>", str(repo_grants.grant_repo(
            mock.Mock(), "s1", "nope", secrets_file=self.root / "s")["error"]))
        with mock.patch.object(repo_grants, "find_forge",
                               return_value=repo_grants.FoundForge(FORGE)), \
                mock.patch("bot_bottle.backend.adoption.read", return_value=None):
            self.assertIn("can't reach", str(repo_grants.grant_repo(
                mock.Mock(), "s1", grant, secrets_file=self.root / "s")["error"]))
        backend = mock.Mock()
        backend.attach_adopted.side_effect = OSError("gone")
        with mock.patch.object(repo_grants, "find_forge",
                               return_value=repo_grants.FoundForge(FORGE)), \
                mock.patch("bot_bottle.backend.adoption.read", return_value=mock.Mock()):
            self.assertIn("host controller's log", str(repo_grants.grant_repo(
                backend, "s1", grant, secrets_file=self.root / "s")["error"]))


class TestKeepGrant(_Root):
    def test_into_a_host_bottle_as_a_version(self) -> None:
        path = self.bottles / "pi.md"
        path.write_text("---\nextends: basic\ngit-gate:\n  repos:\n"
                        "    - gitea.example.com/acme/app\n---\n\nMine.\n")
        keep_grant(KeepTarget(path, None), "gitea.example.com/acme/app:write", extends="gitea")
        keep_grant(KeepTarget(path, None), "gitea.example.com/acme/lib", extends="gitea")
        fm, body = parse_frontmatter(path.read_text())
        self.assertEqual(["gitea.example.com/acme/app:write", "gitea.example.com/acme/lib"],
                         fm["git-gate"]["repos"])  # type: ignore[index]
        self.assertEqual(["basic", "gitea"], fm["extends"])
        self.assertEqual("Mine.", body.strip())

    def test_the_mapping_form_gains_a_named_grant(self) -> None:
        path = self.bottles / "pi.md"
        path.write_text("---\ngit-gate:\n  repos:\n    site: gitea.example.com/acme/web\n---\n")
        keep_grant(KeepTarget(path, None), "gitea.example.com/acme/app")
        fm, _ = parse_frontmatter(path.read_text())
        self.assertEqual({"site": "gitea.example.com/acme/web",
                          "app": "gitea.example.com/acme/app"},
                         fm["git-gate"]["repos"])  # type: ignore[index]
        self.assertNotIn("extends", fm)

    def test_a_new_project_bottle_is_trusted(self) -> None:
        project = self.root / "app"
        path = project / ".bot-bottle" / "bottles" / "main.md"
        with mock.patch("bot_bottle.approvals.trust_project_bottle") as trusted:
            keep_grant(KeepTarget(path, project), "gitea.example.com/acme/app")
        trusted.assert_called_once_with(project, "main")
        fm, _ = parse_frontmatter(path.read_text())
        self.assertEqual("basic", fm["extends"])


def _ask(proposed: str = "gitea.example.com/acme/app:write") -> Request:
    return Request(id="p1", bottle="b1", session="s1", tool="add-repo",
                   justification="need it", proposed=proposed, current="", arrived=ARRIVED,
                   orchestrator="http://orch")


class TestPlainAllowIsRefused(unittest.TestCase):
    def test_only_deny_without_the_card(self) -> None:
        client = mock.Mock()
        with self.assertRaisesRegex(ValueError, "console's card"):
            answer(client, _ask(), ALLOW_ONCE, via="the terminal")
        client.supervise_respond.assert_not_called()
        self.assertEqual("denied", answer(client, _ask(), DENY, via="the terminal"))


class TestRepoCard(unittest.TestCase):
    def test_lines_and_options(self) -> None:
        target = KeepTarget(Path("/x/pi.md"), None)
        with mock.patch.object(repo_grants, "find_forge",
                               return_value=repo_grants.FoundForge(FORGE, "gitea")), \
                mock.patch("bot_bottle.approvals.keep_target", return_value=target):
            lines, options = remote_actions.card(_ask())  # type: ignore[misc]
        self.assertEqual(["add_repo", "add_repo_write", "add_repo_keep"], options)
        text = "\n".join(lines)
        for part in ("repo: gitea.example.com/acme/app", "access: write", "why: need it",
                     "keep: in pi"):
            self.assertIn(part, text)

    def test_read_only_and_nowhere_to_keep(self) -> None:
        with mock.patch.object(repo_grants, "find_forge",
                               return_value=repo_grants.FoundForge(FORGE)), \
                mock.patch("bot_bottle.approvals.keep_target", return_value=None):
            lines, options = remote_actions.repo_attention(_ask("gitea.example.com/acme/app"))
        self.assertEqual(["add_repo"], options)
        self.assertIn("access: read-only", lines)

    def test_unconnected_forge_offers_nothing(self) -> None:
        with mock.patch.object(repo_grants, "find_forge", return_value=None):
            lines, options = remote_actions.repo_attention(_ask())
        self.assertEqual([], options)
        self.assertIn("connect it", lines[-1])

    def test_other_tools_have_no_card(self) -> None:
        self.assertIsNone(remote_actions.card(SimpleNamespace(tool="egress-allow")))


class TestAddRepoAnswer(unittest.TestCase):
    def setUp(self) -> None:
        self.client = mock.Mock()
        self.bottles = mock.Mock()
        self.bottles.grant_repo.return_value = {
            "repo": "app", "url": "ssh://git@h/acme/app.git", "writable": False,
            "extends": "gitea"}
        self.asking: dict[str, Any] = {"a1": _ask()}

    def orchestrator(self, _url: str) -> Any:
        return self.client

    def answer(self, **msg: Any) -> dict[str, Any]:
        return asyncio.run(remote_actions.add_repo_answer(
            {"request_id": "q", "agent_id": "a1", "since": ARRIVED, **msg},
            self.asking, self.orchestrator, self.bottles))

    def test_grants_read_only_unless_write_is_chosen(self) -> None:
        reply = self.answer()
        self.assertEqual(("s1", "gitea.example.com/acme/app"),
                         self.bottles.grant_repo.call_args.args)
        self.assertEqual(("add_repo_answered", "app", []),
                         (reply["type"], reply["repo"], reply["warnings"]))
        notes = self.client.supervise_respond.call_args.kwargs["notes"]
        self.assertIn("(read-only)", notes)
        self.assertIn("ssh://git@h/acme/app.git", notes)
        self.answer(write=True)
        self.assertEqual("gitea.example.com/acme/app:write",
                         self.bottles.grant_repo.call_args.args[1])

    def test_keep(self) -> None:
        target = KeepTarget(Path("/x/pi.md"), None)
        with mock.patch("bot_bottle.approvals.keep_target", return_value=target), \
                mock.patch("bot_bottle.approvals.keep_grant") as kept:
            reply = self.answer(keep=True)
        self.assertEqual("pi", reply["kept"])
        self.assertEqual("gitea", kept.call_args.kwargs["extends"])
        with mock.patch("bot_bottle.approvals.keep_target", return_value=target), \
                mock.patch("bot_bottle.approvals.keep_grant", side_effect=OSError("ro")):
            self.assertIn("keeping it in pi failed", self.answer(keep=True)["warnings"][0])

    def test_failure_leaves_it_waiting(self) -> None:
        self.bottles.grant_repo.return_value = {"error": "no forge"}
        self.assertEqual("no forge", self.answer()["error"])
        self.client.supervise_respond.assert_not_called()
        self.assertIn("can't grant", asyncio.run(remote_actions.add_repo_answer(
            {"agent_id": "a1", "since": ARRIVED}, self.asking, self.orchestrator,
            object()))["error"])

    def test_only_a_waiting_repo_request(self) -> None:
        self.assertIn("no longer waiting", self.answer(since="2000-01-01T00:00:00")["error"])
        self.asking["a1"] = SimpleNamespace(**{**vars(_ask()), "tool": "forge-connect"})
        self.assertIn("no longer waiting", self.answer()["error"])

    def test_answered_elsewhere_is_a_warning(self) -> None:
        self.client.supervise_respond.side_effect = AlreadyAnswered("rejected")
        self.assertIn("answered elsewhere", self.answer()["warnings"][-1])


if __name__ == "__main__":
    unittest.main()


class TestBottleServiceGrant(unittest.TestCase):
    def test_finds_the_backend_the_bottle_runs_on(self) -> None:
        from bot_bottle.control.hostctl.bottles import BottleService  # pylint: disable=import-outside-toplevel
        sessions = mock.MagicMock()
        backend = mock.Mock()
        service = BottleService(sessions, backend_for=lambda _s: backend,
                                manifest_for=lambda _cwd: mock.MagicMock(),
                                secrets_file=Path("/secrets"))
        sessions.record.return_value = None
        self.assertIn("isn't running", str(service.grant_repo("s1", "f/o/r")["error"]))
        sessions.record.return_value = SimpleNamespace(meta={"backend": "docker"})
        with mock.patch.object(repo_grants, "grant_repo", return_value={"repo": "r"}) as grant:
            self.assertEqual({"repo": "r"}, service.grant_repo("s1", "f/o/r"))
        self.assertEqual((backend, "s1", "f/o/r"), grant.call_args.args)
        self.assertEqual(Path("/secrets"), grant.call_args.kwargs["secrets_file"])
