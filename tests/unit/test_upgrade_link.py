"""Unit: the console upgrading bot-bottle on this host (ADR 0022): a newer
release only, signed by a trusted passkey over a challenge bound to it."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.control.hostctl import upgrade_link
from bot_bottle.control.hostctl.upgrade_link import UpgradeError, UpgradeLink, newer
from tests.unit import use_bottle_root
from tests.unit.test_audit_passkey import Passkey, _Tmp


class Versions(_Tmp):
    def test_only_a_newer_release(self) -> None:
        self.assertTrue(newer("0.1.15", "0.1.14"))
        self.assertTrue(newer("0.2.0", "0.1.99"))
        self.assertFalse(newer("0.1.15", "unknown"))  # could be a downgrade
        for target in ("0.1.14", "0.1.13", "main", "v0.1.15", "0.1.15-rc1", "0.1", "",
                       "0.1.15\n", "0.1.\u0661\u0665"):
            self.assertFalse(newer(target, "0.1.14"), target)


class Upgrades(_Tmp):
    def setUp(self) -> None:
        super().setUp()
        self.now = 1000.0
        self.started: list[str] = []
        self.audit = mock.Mock()
        self.tags = {"0.1.15"}
        self.link = UpgradeLink(self._sent, db_path=self.db_path, clock=lambda: self.now,
                                starter=self._start, tag_check=lambda v: v in self.tags,
                                audit=lambda: self.audit)
        self.pk = Passkey()
        self.trust(self.pk)
        self.sent: list[dict[str, Any]] = []
        for target, value in (("install_version", ("0.1.14", "abc")),
                              ("resources.is_source_checkout", False),
                              ("status", {"state": "none"})):
            self.enterContext(mock.patch(f"bot_bottle.control.hostctl.upgrade_link.{target}",
                                         return_value=value))

    async def _sent(self, msg: dict[str, Any]) -> None:
        self.sent.append(msg)

    def _start(self, version: str) -> dict[str, Any]:
        self.started.append(version)
        return {"log": "/x/upgrade.log"}

    def _signed(self, version: str = "0.1.15", session: str = "s1") -> dict[str, Any]:
        ch = self.link.challenge(session, version)
        return self.pk.assertion(str(ch["challenge"]))

    def test_a_signed_upgrade_starts_the_release_and_is_journalled(self) -> None:
        self.assertEqual({"version": "0.1.15"}, self.link.upgrade("s1", self._signed()))
        self.assertEqual(["0.1.15"], self.started)
        self.audit.host_upgrade.assert_called_once_with(
            version="0.1.15", current="0.1.14", credential_id=self.pk.id, error="")

    def test_the_signature_is_bound_to_its_version_session_and_one_use(self) -> None:
        signed = self._signed()
        with self.assertRaisesRegex(UpgradeError, "another session"):
            self.link.upgrade("s2", signed)
        with self.assertRaisesRegex(UpgradeError, "expired or was used"):
            self.link.upgrade("s1", signed)  # spent by the refusal above
        late = self._signed()
        self.now += upgrade_link.CHALLENGE_SECONDS + 1
        with self.assertRaisesRegex(UpgradeError, "expired"):
            self.link.upgrade("s1", late)
        self.assertEqual([], self.started)

    def test_an_untrusted_or_forged_signature_is_refused_and_journalled(self) -> None:
        ch = self.link.challenge("s1", "0.1.15")
        with self.assertRaisesRegex(UpgradeError, "isn't trusted"):
            self.link.upgrade("s1", Passkey(cred_id="other").assertion(str(ch["challenge"])))
        ch = self.link.challenge("s1", "0.1.15")
        with self.assertRaises(UpgradeError):
            self.link.upgrade("s1", self.pk.assertion(str(ch["challenge"]), tamper=True))
        self.assertEqual([], self.started)
        self.assertEqual("", self.audit.host_upgrade.call_args.kwargs["credential_id"])
        self.assertTrue(self.audit.host_upgrade.call_args.kwargs["error"])

    def test_only_a_release_newer_than_this_hosts(self) -> None:
        for version in ("0.1.14", "0.1.13", "main", "1.0.0; rm -rf /", ""):
            with self.assertRaises(UpgradeError, msg=version):
                self.link.challenge("s1", version)
        self.tags = set()  # not a tag on GitHub
        with self.assertRaisesRegex(UpgradeError, "isn't a bot-bottle release"):
            self.link.upgrade("s1", self._signed())
        self.assertEqual([], self.started)

    def test_a_second_signed_upgrade_waits_for_the_first(self) -> None:
        first, second = self._signed(), self._signed()
        self.link.upgrade("s1", first)
        with mock.patch("bot_bottle.control.hostctl.upgrade_link.status",
                        return_value={"state": "running"}):
            with self.assertRaisesRegex(UpgradeError, "already running"):
                self.link.upgrade("s1", second)
        self.assertEqual(["0.1.15"], self.started)

    def test_the_passkey_must_verify_the_user(self) -> None:
        ch = self.link.challenge("s1", "0.1.15")
        with self.assertRaises(UpgradeError):
            self.link.upgrade("s1", self.pk.assertion(str(ch["challenge"]), flags=0x01))
        self.assertEqual([], self.started)

    def test_refusals(self) -> None:
        with self.assertRaisesRegex(UpgradeError, "no console session"):
            self.link.challenge("", "0.1.15")
        with mock.patch("bot_bottle.control.hostctl.upgrade_link.status",
                        return_value={"state": "running"}):
            with self.assertRaisesRegex(UpgradeError, "already running"):
                self.link.challenge("s1", "0.1.15")
        with mock.patch("bot_bottle.control.hostctl.upgrade_link.resources.is_source_checkout",
                        return_value=True):
            with self.assertRaisesRegex(UpgradeError, "source checkout"):
                self.link.challenge("s1", "0.1.15")
        self.trust()
        with self.assertRaisesRegex(UpgradeError, "trusts no passkey"):
            self.link.challenge("s1", "0.1.15")

    def test_the_link_answers_each_request(self) -> None:
        asyncio.run(self.link.handle({"type": "upgrade_challenge", "request_id": "r1",
                                      "session": "s1", "version": "0.1.15"}))
        challenge = self.sent[-1]
        self.assertEqual(("upgrade_challenge", "r1"), (challenge["type"], challenge["request_id"]))
        asyncio.run(self.link.handle({"type": "upgrade", "request_id": "r2", "session": "s1",
                                      "assertion": self.pk.assertion(challenge["challenge"])}))
        self.assertEqual({"type": "upgrade_started", "request_id": "r2", "version": "0.1.15"},
                         self.sent[-1])
        asyncio.run(self.link.handle({"type": "upgrade", "request_id": "r3", "session": "s1"}))
        self.assertEqual("no passkey assertion", self.sent[-1]["error"])
        asyncio.run(self.link.handle({"type": "upgrade_status", "request_id": "r4"}))
        self.assertEqual("none", self.sent[-1]["state"])


class Running(_Tmp):
    """The installer runs on its own, from the release's tag, with none of the
    console's (or the caller's) installer settings."""

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(use_bottle_root(self.dir))

    def test_the_release_installer_runs_detached(self) -> None:
        with mock.patch.dict(os.environ, {"BOT_BOTTLE_INSTALL_SPEC": "evil", "KEEP": "1"}), \
                mock.patch("subprocess.Popen") as popen:
            popen.return_value.pid = 42
            record = upgrade_link.start("0.1.15")
        args, kwargs = popen.call_args
        command = args[0]
        self.assertIn("https://raw.githubusercontent.com/diderisoft/bot-bottle/v0.1.15/install.sh",
                      command)
        self.assertIn("v0.1.15", command)
        self.assertIn("bot-bottle up --restart --yes", command[2])  # adopts running bottles
        self.assertTrue(kwargs["start_new_session"])
        self.assertNotIn("BOT_BOTTLE_INSTALL_SPEC", kwargs["env"])
        self.assertEqual("1", kwargs["env"]["KEEP"])
        self.assertEqual("0.1.15", record["version"])

    def test_status_follows_the_installer(self) -> None:
        self.assertEqual({"state": "none"}, upgrade_link.status())
        with mock.patch("subprocess.Popen") as popen:
            popen.return_value.pid = os.getpid()  # alive
            record = upgrade_link.start("0.1.15")
        self.assertEqual("running", upgrade_link.status()["state"])
        Path(record["log"]).write_text("fetching\nerror: no network\n", encoding="utf-8")
        Path(record["exit"]).write_text("1\n", encoding="utf-8")
        failed = upgrade_link.status()
        self.assertEqual(("failed", "0.1.15"), (failed["state"], failed["version"]))
        self.assertIn("no network", failed["log"])
        Path(record["exit"]).write_text("0\n", encoding="utf-8")
        self.assertEqual("done", upgrade_link.status()["state"])

    def test_an_interrupted_upgrade_doesnt_block_the_next(self) -> None:
        with mock.patch("subprocess.Popen") as popen:
            popen.return_value.pid = 2 ** 22 + 7  # no such process
            upgrade_link.start("0.1.15")
        state = upgrade_link.status()
        self.assertEqual("failed", state["state"])
        self.assertIn("interrupted", state["log"])
