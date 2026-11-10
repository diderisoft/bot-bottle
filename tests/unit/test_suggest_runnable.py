"""suggest.runnable: a proposal is made runnable on this host before anyone is
asked to run it (bottles whose secrets aren't here are dropped, or the
proposal is blocked). A repo grant needs no forge bottle stacked: it
resolves through the host's connection (ADR 0019)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot_bottle.errors import SecretsFileError
from bot_bottle.forge_connections import ForgeConnection, ForgeUser
from bot_bottle.manifest.loader import BUILTIN_BOTTLES_DIR
from bot_bottle.secret_source import MappingSecrets, SecretSource
from bot_bottle.suggest import runnable
from bot_bottle.suggest.proposal import (
    OPTION_NEW_EXTENDING, OPTION_STACK_ADDITIONS, Proposal, ProposalAdditions,
    ProposalNewBottle, ProposalRepo,
)
from bot_bottle.suggest.render import render_proposal
from bot_bottle.suggest.validate import validate_proposal
from tests.unit._forges import connected

GRANT = "gitea.dideric.is/didericis-claude/flaky-api-demo:write"
_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"

CONN = ForgeConnection(host="gitea.dideric.is", kind="gitea", url="https://gitea.dideric.is",
                       ssh="ssh://git@gitea.dideric.is:30009", host_key=_KEY,
                       users=(ForgeUser("didericis", "GITEA_DIDERIC_IS_TOKEN", default=True),))

_BOTTLES = {
    "claude-dev": """---
extends: basic
egress:
  routes:
    - host: gitea.dideric.is
      inspect:
        auth:
          type: bearer
          value: secret:BOT_BOTTLE_CLAUDE_GITEA_TOKEN
---

Claude, developing bot-bottle.
""",
    "bot-bottle-dev": "---\nextends: basic\n---\n\nbot-bottle work.\n",
    "tools": "---\negress:\n  routes:\n    - host: tools.example.com\n---\n\nTools.\n",
}

FORGE_TOKEN = "GITEA_DIDERIC_IS_TOKEN"


def _proposal(bottles: list[str], grants: tuple[str, ...] = (GRANT,), *,
              option: str = OPTION_STACK_ADDITIONS,
              new: ProposalNewBottle | None = None) -> Proposal:
    return Proposal(option=option, bottles=list(bottles), new_bottle=new,
                    additions=ProposalAdditions(repos=[ProposalRepo(grant=g) for g in grants]),
                    similar=[], notes=[])


class _Library(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(connected(CONN))
        # pylint: disable-next=consider-using-with
        self.host = Path(self.enterContext(tempfile.TemporaryDirectory())) / "bottles"
        self.host.mkdir()
        for name, text in _BOTTLES.items():
            (self.host / f"{name}.md").write_text(text)
        self.dirs = [(self.host, False), (BUILTIN_BOTTLES_DIR, False)]
        self.secrets: SecretSource = MappingSecrets({FORGE_TOKEN: "x"})

    def validate(self, proposal: Proposal) -> Proposal:
        return validate_proposal(proposal, self.dirs, self.secrets)[0]


class TestDevScenario(_Library):
    def test_the_flaky_test_task_ends_up_runnable_with_nothing_stacked(self) -> None:
        """The dry run's proposal: `claude-dev` plus a write grant on a gitea
        repo. claude-dev's secret isn't here, so it goes; the grant needs no
        forge bottle, so nothing else is stacked and it stays."""
        out = self.validate(_proposal(["claude-dev"]))
        self.assertEqual(["basic"], out.bottles)
        self.assertEqual([GRANT], [r.grant for r in out.additions.repos])
        self.assertEqual("", out.blocked)
        notes = "\n".join(out.notes)
        self.assertNotIn("stacked gitea", notes)
        self.assertIn("dropped claude-dev: its secret BOT_BOTTLE_CLAUDE_GITEA_TOKEN isn't set",
                      notes)
        self.assertNotIn("dropped repo grant", notes)

    def test_with_the_secret_set_claude_dev_stays(self) -> None:
        self.secrets = MappingSecrets({FORGE_TOKEN: "x", "BOT_BOTTLE_CLAUDE_GITEA_TOKEN": "y"})
        out = self.validate(_proposal(["claude-dev"]))
        self.assertEqual(["claude-dev"], out.bottles)


class TestForges(_Library):
    def test_a_stack_is_left_as_it_is(self) -> None:
        out = self.validate(_proposal(["bot-bottle-dev"]))
        self.assertEqual(["bot-bottle-dev"], out.bottles)
        self.assertEqual([GRANT], [r.grant for r in out.additions.repos])

    def test_a_stack_without_basic_in_its_chain_gets_basic_first(self) -> None:
        """A stack of bottles that extend nothing has no PyPI: a suggested
        stack starts from basic."""
        out = self.validate(_proposal(["tools"]))
        self.assertEqual(["basic", "tools"], out.bottles)
        self.assertTrue(any(n.startswith("stacked basic:") for n in out.notes))
        self.assertEqual([GRANT], [r.grant for r in out.additions.repos])

    def test_a_stack_already_on_basic_is_left_alone(self) -> None:
        out = self.validate(_proposal(["basic", "tools"]))
        self.assertEqual(["basic", "tools"], out.bottles)
        self.assertFalse(any(n.startswith("stacked basic:") for n in out.notes))

    def test_the_compact_preview_lists_additions_and_counts_the_rest(self) -> None:
        out = self.validate(_proposal(["basic", "tools"]))
        text = render_proposal(out, self.dirs, compact=True)
        preview = text.split("elements preview:", 1)[1].splitlines()
        self.assertTrue(any("flaky-api-demo" in line and "[this proposal]" in line
                            for line in preview))
        self.assertTrue(any(line.strip().startswith("basic:") and "egress" in line
                            for line in preview))
        self.assertTrue(any(line.strip().startswith("tools:") for line in preview))
        self.assertFalse(any("pypi.org" in line for line in preview))
        full = render_proposal(out, self.dirs).split("elements preview:", 1)[1]
        self.assertIn("pypi.org", full)

    def test_a_grant_on_a_forge_not_connected_is_dropped(self) -> None:
        out = self.validate(_proposal(["basic"], ("github.com/acme/app",)))
        self.assertEqual(["basic"], out.bottles)
        self.assertEqual([], out.additions.repos)
        self.assertIn("dropped repo grant 'github.com/acme/app'", "\n".join(out.notes))
        self.assertIn("isn't connected on this host", "\n".join(out.notes))

    def test_a_grant_whose_forge_token_is_missing_blocks_it(self) -> None:
        self.secrets = MappingSecrets({})
        out = self.validate(_proposal(["basic"]))
        self.assertEqual(f"the repo grant {GRANT} needs the forge token {FORGE_TOKEN}, "
                         f"which isn't set on this host (host env or secrets file)", out.blocked)


class TestAdditionsBottle(_Library):
    def test_a_repo_grant_brings_its_forges_description_not_a_parent(self) -> None:
        from bot_bottle.suggest.validate import proposal_to_frontmatter
        fm = proposal_to_frontmatter(self.validate(_proposal(["claude-dev"])))
        self.assertNotIn("extends", fm)
        self.assertEqual({"gitea.dideric.is": {"kind": "gitea",
                                               "ssh": "ssh://git@gitea.dideric.is:30009"}},
                         fm["git-gate"]["forges"])
        self.assertEqual([GRANT], fm["git-gate"]["repos"])

    def test_without_grants_it_stays_additions_only(self) -> None:
        from bot_bottle.suggest.validate import proposal_to_frontmatter
        self.assertNotIn("extends", proposal_to_frontmatter(_proposal(["tools"], ())))


class TestSecrets(_Library):
    def test_a_new_bottle_on_a_base_that_cant_run_is_blocked(self) -> None:
        out = self.validate(_proposal([], (), option=OPTION_NEW_EXTENDING,
                                      new=ProposalNewBottle(name="web", extends="claude-dev")))
        self.assertIn("claude-dev needs BOT_BOTTLE_CLAUDE_GITEA_TOKEN", out.blocked)
        self.assertIn("can't run: claude-dev needs", render_proposal(out))

    def test_blocked_rides_the_dict_but_the_model_cant_set_it(self) -> None:
        out = self.validate(_proposal([], (), option=OPTION_NEW_EXTENDING,
                                      new=ProposalNewBottle(name="web", extends="claude-dev")))
        self.assertEqual(out.blocked, out.to_dict()["blocked"])
        self.assertEqual("", Proposal.from_dict(out.to_dict()).blocked)
        self.assertNotIn("blocked", self.validate(_proposal(["basic"], ())).to_dict())

    def test_library_facts_name_missing_secrets(self) -> None:
        self.assertEqual([], runnable.library_facts("tools", self.dirs, self.secrets))
        self.assertEqual(["BOT_BOTTLE_CLAUDE_GITEA_TOKEN"],
                         runnable.library_facts("claude-dev", self.dirs, self.secrets))
        self.assertEqual([], runnable.library_facts("nope", self.dirs, self.secrets))

    def test_an_unreadable_secrets_file_counts_as_unset(self) -> None:
        class Broken(SecretSource):
            def get(self, name: str) -> str | None:
                raise SecretsFileError("bad permissions")
        self.assertFalse(runnable.has_secret(Broken(), "ANY"))

    def test_host_secrets_read_env_then_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secrets"
            path.write_text("FROM_FILE=1\n")
            path.chmod(0o600)
            source = runnable.host_secrets({"FROM_ENV": "1"}, path)
            self.assertTrue(runnable.has_secret(source, "FROM_ENV"))
            self.assertTrue(runnable.has_secret(source, "FROM_FILE"))
            self.assertFalse(runnable.has_secret(source, "NEITHER"))


if __name__ == "__main__":
    unittest.main()
