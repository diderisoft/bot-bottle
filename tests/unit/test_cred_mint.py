"""Tests for cred_mint.mint_cred_block, adoption.cred_block_for,
and BottlePlan.minted_cred_values."""

from __future__ import annotations

import dataclasses
import unittest
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.agent_provider import AgentProvisionPlan
from bot_bottle.backend import BottleSpec
from bot_bottle.backend.adoption import (
    AdoptedCredBlock, AdoptedCredEntry, AdoptionRecord, cred_block_for,
)
from bot_bottle.backend.firecracker.bottle_plan import FirecrackerBottlePlan
from bot_bottle.credentials import (
    CredentialProvider, CredentialRegistry, Grant, KIND_TOKEN, MintedCredential,
)
from bot_bottle.egress import EgressPlan
from bot_bottle.git_gate import GitGatePlan
from bot_bottle.manifest import ManifestIndex
from bot_bottle.secret_source import MappingSecrets
from tests.unit import use_bottle_root

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
EXPIRES = NOW + timedelta(hours=1)
_SLUG = "demo-cred-mint-1"

# "host_login" is the well-known source that the manifest validates against the
# registry.  Tests that need a fake provider inject it under "host_login" into a
# purpose-built CredentialRegistry so the manifest accepts the source name but
# mint_cred_block calls our fake.
_FAKE_SOURCE = "host_login"


def _no_file_secrets(_path: object) -> MappingSecrets:
    """Stands in for FileSecrets: the host secrets file is empty."""
    return MappingSecrets({})


def _index(bottle_extra: dict[str, Any] | None = None) -> ManifestIndex:
    bottle_def: dict[str, object] = {}
    if bottle_extra:
        bottle_def.update(bottle_extra)
    return ManifestIndex.from_json_obj({
        "bottles": {"dev": bottle_def},
        "agents": {"demo": {"skills": [], "prompt": ""}},
    })


def _stage_fields(stage: Path, bottle_extra: dict[str, Any] | None = None) -> dict[str, Any]:
    idx = _index(bottle_extra)
    return {
        "spec": BottleSpec(manifest=idx, agent_name="demo", bottle_names=("dev",),
                           copy_cwd=False, user_cwd=str(stage), identity=_SLUG),
        "manifest": idx.load_for_agent("demo", ("dev",)),
        "stage_dir": stage,
        "git_gate_plan": GitGatePlan(
            slug=_SLUG, hook_script=stage / "h.sh",
            access_hook_script=stage / "a.sh", upstreams=()),
        "egress_plan": EgressPlan(
            slug=_SLUG, routes_path=stage / "egress.yaml", routes=(),
            token_env_map={}),
        "supervise_plan": None,
        "agent_provision": AgentProvisionPlan(
            harness="claude", command="claude", prompt_mode="append_file",
            image="bot-bottle-claude:latest", dockerfile="", guest_home="/home/node",
            instance_name=f"bot-bottle-{_SLUG}", prompt_file=stage / "prompt.txt",
            guest_env={}, provisioned_env={}),
        "settings": mock.Mock(),
        "secrets": MappingSecrets({}),
        "slug": _SLUG,
        "forwarded_env": {},
    }


class _FakeMintProvider(CredentialProvider):
    """Fake provider registered under 'host_login' so the manifest validates."""
    source = _FAKE_SOURCE
    kinds = (KIND_TOKEN,)
    project_definable = True

    def __init__(self, token: str = "minted-token",
                 expires_at: datetime | None = None) -> None:
        self._token = token
        self._expires_at = expires_at
        self.mint_calls: list[tuple[Any, ...]] = []

    def mint(self, label: str, bottle_id: str,
             settings: Mapping[str, object], secrets: object,
             *, minted_creds: Mapping[str, MintedCredential] | None = None,
             ) -> MintedCredential:
        self.mint_calls.append((label, bottle_id, settings))
        return MintedCredential(KIND_TOKEN, {"token": self._token}, self._expires_at,
                                f"fake:{label}")

    def describe(self, settings: Mapping[str, object]) -> Grant:
        return Grant("", "", "", "", "fake")


class _FailingProvider(CredentialProvider):
    """Provider registered under 'host_login' that always raises on mint."""
    source = _FAKE_SOURCE
    kinds = (KIND_TOKEN,)
    project_definable = True

    def mint(self, label: str, bottle_id: str,
             settings: Mapping[str, object], secrets: object,
             *, minted_creds: Mapping[str, MintedCredential] | None = None,
             ) -> MintedCredential:
        raise ValueError("simulated mint failure")

    def describe(self, settings: Mapping[str, object]) -> Grant:
        return Grant("", "", "", "", "fail")


def _registry_with(*providers: CredentialProvider) -> CredentialRegistry:
    reg = CredentialRegistry(contrib_root=Path("/nonexistent"))
    for p in providers:
        reg._providers[p.source] = p  # type: ignore[attr-defined]
    return reg


# ── mint_cred_block ────────────────────────────────────────────────────────────

class TestMintCredBlock(unittest.TestCase):

    def setUp(self) -> None:
        import tempfile, shutil
        self.stage = Path(tempfile.mkdtemp(prefix="cred-mint-test."))
        self.addCleanup(shutil.rmtree, self.stage, ignore_errors=True)
        self.addCleanup(use_bottle_root(self.stage / "root"))

    def _plan(self, bottle_extra: dict[str, Any] | None = None,
              token_env_map: dict[str, Any] | None = None) -> FirecrackerBottlePlan:
        fields = _stage_fields(self.stage, bottle_extra)
        plan = FirecrackerBottlePlan(**fields)
        if token_env_map is not None:
            plan = dataclasses.replace(
                plan, egress_plan=dataclasses.replace(
                    plan.egress_plan, token_env_map=token_env_map))
        return plan

    def test_no_credentials_block_returns_unchanged(self) -> None:
        from bot_bottle.control.hostctl.cred_mint import mint_cred_block
        plan = self._plan()
        updated, entries = mint_cred_block(plan, _SLUG, MappingSecrets({}))
        self.assertIs(updated, plan)
        self.assertEqual(entries, [])

    def test_secret_source_skipped(self) -> None:
        from bot_bottle.control.hostctl.cred_mint import mint_cred_block
        prov = _FakeMintProvider()
        reg = _registry_with(prov)
        plan = self._plan(
            bottle_extra={"credentials": {"my-cred": {"source": "secret", "secret": "MY_SECRET"}}},
            token_env_map={"EGRESS_TOKEN_0": "MY_SECRET"},
        )
        with mock.patch("bot_bottle.secret_source.FileSecrets", _no_file_secrets):
            updated, entries = mint_cred_block(plan, _SLUG, MappingSecrets({}), registry=reg)
        self.assertIs(updated, plan)
        self.assertEqual(entries, [])
        self.assertEqual(prov.mint_calls, [])

    def test_fake_mint_provider_mints_and_injects(self) -> None:
        from bot_bottle.control.hostctl.cred_mint import mint_cred_block
        prov = _FakeMintProvider(token="live-token")
        reg = _registry_with(prov)
        plan = self._plan(
            bottle_extra={"credentials": {"mylogin": {"source": _FAKE_SOURCE}}},
            token_env_map={"EGRESS_TOKEN_0": "__cred__mylogin"},
        )
        with mock.patch("bot_bottle.secret_source.FileSecrets", _no_file_secrets):
            updated, entries = mint_cred_block(plan, _SLUG, MappingSecrets({}), registry=reg)
        self.assertIsNot(updated, plan)
        self.assertEqual(updated.minted_cred_values.get("__cred__mylogin"), "live-token")
        self.assertEqual(len(entries), 1)
        label, minted, _prov, _settings, _sec, slots = entries[0]
        self.assertEqual(label, "mylogin")
        self.assertEqual(minted.values["token"], "live-token")
        self.assertEqual(slots["token"], ("EGRESS_TOKEN_0",))
        self.assertEqual(len(prov.mint_calls), 1)
        self.assertEqual(prov.mint_calls[0][0], "mylogin")

    def test_mint_failure_propagates(self) -> None:
        from bot_bottle.control.hostctl.cred_mint import mint_cred_block
        prov = _FailingProvider()
        reg = _registry_with(prov)
        plan = self._plan(
            bottle_extra={"credentials": {"bad": {"source": _FAKE_SOURCE}}},
        )
        with mock.patch("bot_bottle.secret_source.FileSecrets", _no_file_secrets):
            with self.assertRaises(ValueError):
                mint_cred_block(plan, _SLUG, MappingSecrets({}), registry=reg)

    def test_minted_values_appear_in_egress_secrets(self) -> None:
        from bot_bottle.control.hostctl.cred_mint import mint_cred_block
        prov = _FakeMintProvider(token="injected")
        reg = _registry_with(prov)
        plan = self._plan(
            bottle_extra={"credentials": {"tok": {"source": _FAKE_SOURCE}}},
            token_env_map={"EGRESS_TOKEN_0": "__cred__tok"},
        )
        with mock.patch("bot_bottle.secret_source.FileSecrets", _no_file_secrets):
            updated, _ = mint_cred_block(plan, _SLUG, MappingSecrets({}), registry=reg)
        self.assertEqual(updated.egress_secrets.get("__cred__tok"), "injected")

    def test_multiple_slots_per_credential(self) -> None:
        from bot_bottle.control.hostctl.cred_mint import mint_cred_block
        prov = _FakeMintProvider(token="tok")
        reg = _registry_with(prov)
        plan = self._plan(
            bottle_extra={"credentials": {"multi": {"source": _FAKE_SOURCE}}},
            token_env_map={
                "EGRESS_TOKEN_0": "__cred__multi",
                "EGRESS_TOKEN_1": "__cred__multi",
                "EGRESS_TOKEN_2": "OTHER",
            },
        )
        with mock.patch("bot_bottle.secret_source.FileSecrets", _no_file_secrets):
            _, entries = mint_cred_block(plan, _SLUG, MappingSecrets({}), registry=reg)
        slots = entries[0][5]
        self.assertIn("EGRESS_TOKEN_0", slots["token"])
        self.assertIn("EGRESS_TOKEN_1", slots["token"])
        self.assertNotIn("EGRESS_TOKEN_2", slots["token"])

    def test_secret_ssh_key_minted_into_minted_ssh_keys(self) -> None:
        # source: secret, kind: ssh-key must NOT be skipped — there is no
        # EgressTokenSecrets path for ssh keys; the values must reach provision.py.
        from bot_bottle.control.hostctl.cred_mint import mint_cred_block
        from bot_bottle.credentials import _SecretProvider
        reg = _registry_with(_SecretProvider())
        secrets = MappingSecrets({"PRIV": "priv-pem-content", "PUB": "ssh-ed25519 AAAA..."})
        plan = self._plan(
            bottle_extra={"credentials": {"deploy-key": {
                "source": "secret", "kind": "ssh-key",
                "private_key": "PRIV", "public_key": "PUB",
            }}},
        )
        with mock.patch("bot_bottle.secret_source.FileSecrets", _no_file_secrets):
            updated, entries = mint_cred_block(plan, _SLUG, secrets, registry=reg)
        self.assertIsNot(updated, plan)
        self.assertIn("deploy-key", updated.minted_ssh_keys)
        self.assertEqual(updated.minted_ssh_keys["deploy-key"]["private_key"], "priv-pem-content")
        self.assertEqual(updated.minted_ssh_keys["deploy-key"]["public_key"], "ssh-ed25519 AAAA...")
        self.assertEqual(len(entries), 1)

    def test_secret_token_still_skipped(self) -> None:
        # source: secret, kind: token must still be skipped (EgressTokenSecrets handles it).
        from bot_bottle.control.hostctl.cred_mint import mint_cred_block
        from bot_bottle.credentials import _SecretProvider
        reg = _registry_with(_SecretProvider())
        plan = self._plan(
            bottle_extra={"credentials": {"my-tok": {"source": "secret", "secret": "MY_SECRET"}}},
            token_env_map={"EGRESS_TOKEN_0": "MY_SECRET"},
        )
        with mock.patch("bot_bottle.secret_source.FileSecrets", _no_file_secrets):
            updated, entries = mint_cred_block(plan, _SLUG, MappingSecrets({}), registry=reg)
        self.assertIs(updated, plan)
        self.assertEqual(entries, [])

    def test_no_matching_slot_still_mints(self) -> None:
        from bot_bottle.control.hostctl.cred_mint import mint_cred_block
        prov = _FakeMintProvider(token="env-only")
        reg = _registry_with(prov)
        plan = self._plan(
            bottle_extra={"credentials": {"env-cred": {"source": _FAKE_SOURCE}}},
            token_env_map={},  # no egress slots
        )
        with mock.patch("bot_bottle.secret_source.FileSecrets", _no_file_secrets):
            _, entries = mint_cred_block(plan, _SLUG, MappingSecrets({}), registry=reg)
        # Minted, but no slots and no value in token_env_map.
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][5], {})  # empty slots


# ── cred_block_for ─────────────────────────────────────────────────────────────

class TestCredBlockFor(unittest.TestCase):

    def setUp(self) -> None:
        import tempfile, shutil
        self.stage = Path(tempfile.mkdtemp(prefix="cred-adopt-test."))
        self.addCleanup(shutil.rmtree, self.stage, ignore_errors=True)
        self.addCleanup(use_bottle_root(self.stage / "root"))

    def _plan(self, bottle_extra: dict[str, Any] | None = None,
              token_env_map: dict[str, Any] | None = None) -> FirecrackerBottlePlan:
        fields = _stage_fields(self.stage, bottle_extra)
        plan = FirecrackerBottlePlan(**fields)
        if token_env_map is not None:
            plan = dataclasses.replace(
                plan, egress_plan=dataclasses.replace(
                    plan.egress_plan, token_env_map=token_env_map))
        return plan

    def test_no_credentials_returns_none(self) -> None:
        self.assertIsNone(cred_block_for(self._plan()))

    def test_only_secret_source_returns_none(self) -> None:
        plan = self._plan(
            bottle_extra={"credentials": {"s": {"source": "secret", "secret": "X"}}},
            token_env_map={"EGRESS_TOKEN_0": "X"},
        )
        self.assertIsNone(cred_block_for(plan))

    def test_host_login_source_stored(self) -> None:
        plan = self._plan(
            bottle_extra={"credentials": {"mylogin": {"source": _FAKE_SOURCE}}},
            token_env_map={"EGRESS_TOKEN_0": "__cred__mylogin"},
        )
        result = cred_block_for(plan)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(len(result.entries), 1)
        entry = result.entries[0]
        self.assertEqual(entry.label, "mylogin")
        self.assertEqual(entry.source, _FAKE_SOURCE)
        self.assertEqual(entry.kind, KIND_TOKEN)
        self.assertEqual(entry.slots.get("token"), ["EGRESS_TOKEN_0"])

    def test_no_matching_slot_excluded(self) -> None:
        plan = self._plan(
            bottle_extra={"credentials": {"orphan": {"source": _FAKE_SOURCE}}},
            token_env_map={},
        )
        self.assertIsNone(cred_block_for(plan))


# ── cred_block_for ssh-key entries ────────────────────────────────────────────

class TestCredBlockForSshKey(unittest.TestCase):

    def setUp(self) -> None:
        import tempfile, shutil
        self.stage = Path(tempfile.mkdtemp(prefix="cred-adopt-ssh-test."))
        self.addCleanup(shutil.rmtree, self.stage, ignore_errors=True)
        self.addCleanup(use_bottle_root(self.stage / "root"))

    def _plan(self, bottle_extra: dict[str, Any] | None = None,
              token_env_map: dict[str, Any] | None = None) -> FirecrackerBottlePlan:
        fields = _stage_fields(self.stage, bottle_extra)
        plan = FirecrackerBottlePlan(**fields)
        if token_env_map is not None:
            plan = dataclasses.replace(
                plan, egress_plan=dataclasses.replace(
                    plan.egress_plan, token_env_map=token_env_map))
        return plan

    def test_ssh_key_cred_included_with_git_names(self) -> None:
        plan = self._plan(bottle_extra={
            "credentials": {"my-key": {
                "source": "secret", "kind": "ssh-key",
                "private_key": "PRIV", "public_key": "PUB",
            }},
            "git_gate": {"repos": {"app": {
                "url": "ssh://git@github.com/owner/repo.git",
                "key": "@my-key",
            }}},
        })
        result = cred_block_for(plan)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(len(result.entries), 1)
        e = result.entries[0]
        self.assertEqual("my-key", e.label)
        self.assertEqual("ssh-key", e.kind)
        self.assertEqual(("app",), e.git_entry_names)
        self.assertEqual({}, e.slots)

    def test_ssh_key_multiple_repos(self) -> None:
        plan = self._plan(bottle_extra={
            "credentials": {"shared-key": {
                "source": "file",
                "private_key_path": "/priv", "public_key_path": "/pub",
            }},
            "git_gate": {"repos": {
                "app": {"url": "ssh://git@github.com/owner/app.git", "key": "@shared-key"},
                "docs": {"url": "ssh://git@github.com/owner/docs.git", "key": "@shared-key"},
            }},
        })
        result = cred_block_for(plan)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(1, len(result.entries))
        self.assertIn("app", result.entries[0].git_entry_names)
        self.assertIn("docs", result.entries[0].git_entry_names)

    def test_ssh_key_with_no_git_refs_excluded(self) -> None:
        # An ssh-key credential that no git entry references has nothing to adopt.
        plan = self._plan(bottle_extra={
            "credentials": {"unused-key": {
                "source": "secret", "kind": "ssh-key",
                "private_key": "PRIV", "public_key": "PUB",
            }},
        })
        self.assertIsNone(cred_block_for(plan))

    def test_secret_token_still_excluded(self) -> None:
        plan = self._plan(
            bottle_extra={"credentials": {"tok": {"source": "secret", "secret": "X"}}},
            token_env_map={"EGRESS_TOKEN_0": "X"},
        )
        self.assertIsNone(cred_block_for(plan))


# ── _rewrite_cred_key_files ────────────────────────────────────────────────────

class TestRewriteCredKeyFiles(unittest.TestCase):

    def setUp(self) -> None:
        import tempfile, shutil
        self.tmp = Path(tempfile.mkdtemp(prefix="rewrite-key-test."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _entry(self, git_names: tuple[str, ...]) -> AdoptedCredEntry:
        return AdoptedCredEntry(
            label="my-key", source="file", kind="ssh-key",
            settings={"private_key_path": "/priv", "kind": "ssh-key"},
            slots={}, git_entry_names=git_names,
        )

    def _minted(self, private_key: str = "priv-pem-content") -> MintedCredential:
        return MintedCredential("ssh-key",
                                {"private_key": private_key, "public_key": "pub"},
                                None, "file:/priv")

    def test_writes_key_file_per_git_entry(self) -> None:
        from bot_bottle.control.hostctl.cred_mint import _rewrite_cred_key_files
        gate_dir = self.tmp / "gate"
        gate_dir.mkdir()
        entry = self._entry(("app", "docs"))
        with mock.patch("bot_bottle.bottle_state.git_gate_state_dir",
                        return_value=gate_dir):
            _rewrite_cred_key_files("test-slug", entry, self._minted())
        self.assertTrue((gate_dir / "app-key").exists())
        self.assertEqual("priv-pem-content", (gate_dir / "app-key").read_text())
        self.assertEqual(0o600, (gate_dir / "app-key").stat().st_mode & 0o777)
        self.assertTrue((gate_dir / "docs-key").exists())

    def test_creates_stage_dir_if_missing(self) -> None:
        from bot_bottle.control.hostctl.cred_mint import _rewrite_cred_key_files
        gate_dir = self.tmp / "missing" / "gate"
        with mock.patch("bot_bottle.bottle_state.git_gate_state_dir",
                        return_value=gate_dir):
            _rewrite_cred_key_files("test-slug", self._entry(("repo",)), self._minted())
        self.assertTrue((gate_dir / "repo-key").exists())

    def test_no_git_names_does_nothing(self) -> None:
        from bot_bottle.control.hostctl.cred_mint import _rewrite_cred_key_files
        # Should not raise or touch the filesystem.
        _rewrite_cred_key_files("slug", self._entry(()), self._minted())

    def test_missing_private_key_warns_and_skips(self) -> None:
        from bot_bottle.control.hostctl.cred_mint import _rewrite_cred_key_files
        from bot_bottle.credentials import KIND_SSH_KEY, MintedCredential
        minted = MintedCredential(KIND_SSH_KEY, {"private_key": "", "public_key": "pub"},
                                  None, "file:/priv")
        gate_dir = self.tmp / "gate2"
        gate_dir.mkdir()
        with mock.patch("bot_bottle.bottle_state.git_gate_state_dir", return_value=gate_dir):
            _rewrite_cred_key_files("slug", self._entry(("repo",)), minted)
        self.assertFalse((gate_dir / "repo-key").exists())


# ── recredential_block ssh-key path ───────────────────────────────────────────

class TestRecredentialBlockSshKey(unittest.TestCase):

    def setUp(self) -> None:
        import tempfile, shutil
        self.tmp = Path(tempfile.mkdtemp(prefix="recred-ssh-test."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_re_mints_and_writes_key_file(self) -> None:
        from bot_bottle.control.hostctl.cred_mint import recredential_block
        from bot_bottle.backend.adoption import AdoptedCredBlock, AdoptedCredEntry, AdoptionRecord
        from bot_bottle.credentials import _SecretProvider, KIND_SSH_KEY

        gate_dir = self.tmp / "gate"
        gate_dir.mkdir()

        entry = AdoptedCredEntry(
            label="my-key", source="secret", kind=KIND_SSH_KEY,
            settings={"private_key": "PRIV_SECRET", "public_key": "PUB_SECRET",
                      "kind": KIND_SSH_KEY},
            slots={}, git_entry_names=("app",),
        )
        record_obj = AdoptionRecord(
            container="cnt", registry_id="reg-id",
            cred_block=AdoptedCredBlock(entries=(entry,)),
        )
        record = mock.Mock()
        record.id = _SLUG

        secrets = MappingSecrets({"PRIV_SECRET": "-----BEGIN OPENSSH PRIVATE KEY-----\npriv\n",
                                  "PUB_SECRET": "ssh-ed25519 AAAA..."})
        reg = _registry_with(_SecretProvider())

        with mock.patch("bot_bottle.backend.adoption.read", return_value=record_obj), \
             mock.patch("bot_bottle.secret_source.FileSecrets", mock.Mock(return_value=secrets)), \
             mock.patch("bot_bottle.bottle_state.git_gate_state_dir", return_value=gate_dir), \
             mock.patch("bot_bottle.control.hostctl.credential_refresh.read_env_var_secret",
                        return_value="env-sec"):
            recredential_block(record, mock.Mock(attach_adopted=mock.Mock(return_value=mock.Mock())),
                               mock.Mock(), mock.Mock(), registry=reg)

        key_file = gate_dir / "app-key"
        self.assertTrue(key_file.exists(), "key file was not written on restart")
        self.assertIn("PRIVATE KEY", key_file.read_text())
        self.assertEqual(0o600, key_file.stat().st_mode & 0o777)


# ── adoption serialisation round-trip ──────────────────────────────────────────

class TestAdoptedCredBlockRoundTrip(unittest.TestCase):

    def test_round_trip_through_json(self) -> None:
        import json
        from bot_bottle.backend import adoption
        entry = AdoptedCredEntry(
            label="mylogin", source="host_login", kind=KIND_TOKEN,
            settings={"harness": "claude"},
            slots={"token": ["EGRESS_TOKEN_0", "EGRESS_TOKEN_1"]},
        )
        block = AdoptedCredBlock(entries=(entry,))
        raw = json.loads(json.dumps(dataclasses.asdict(block)))
        result = adoption._cred_block(raw)  # type: ignore[attr-defined]
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(len(result.entries), 1)
        e = result.entries[0]
        self.assertEqual(e.label, "mylogin")
        self.assertEqual(e.source, "host_login")
        self.assertEqual(e.slots["token"], ["EGRESS_TOKEN_0", "EGRESS_TOKEN_1"])

    def test_none_returns_none(self) -> None:
        from bot_bottle.backend import adoption
        self.assertIsNone(adoption._cred_block(None))  # type: ignore[attr-defined]

    def test_bad_entry_skipped(self) -> None:
        from bot_bottle.backend import adoption
        raw = {
            "entries": [
                {"label": "ok", "source": "host_login", "kind": "token",
                 "settings": {}, "slots": {}},
                {"broken": True},
            ]
        }
        result = adoption._cred_block(raw)  # type: ignore[attr-defined]
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.entries[0].label, "ok")

    def test_missing_entries_key_returns_none(self) -> None:
        from bot_bottle.backend import adoption
        self.assertIsNone(adoption._cred_block({"no_entries": []}))  # type: ignore[attr-defined]

    def test_round_trip_with_git_entry_names(self) -> None:
        import json
        from bot_bottle.backend import adoption
        entry = AdoptedCredEntry(
            label="deploy-key", source="file", kind="ssh-key",
            settings={"private_key_path": "/priv", "public_key_path": "/pub",
                      "kind": "ssh-key"},
            slots={},
            git_entry_names=("app", "docs"),
        )
        block = AdoptedCredBlock(entries=(entry,))
        raw = json.loads(json.dumps(dataclasses.asdict(block)))
        result = adoption._cred_block(raw)  # type: ignore[attr-defined]
        self.assertIsNotNone(result)
        assert result is not None
        e = result.entries[0]
        self.assertEqual(e.git_entry_names, ("app", "docs"))
        self.assertEqual(e.kind, "ssh-key")
        self.assertEqual(e.slots, {})


# ── BottlePlan.minted_cred_values ─────────────────────────────────────────────

class TestPlanMintedCredValues(unittest.TestCase):

    def setUp(self) -> None:
        import tempfile, shutil
        self.stage = Path(tempfile.mkdtemp(prefix="cred-plan-test."))
        self.addCleanup(shutil.rmtree, self.stage, ignore_errors=True)
        self.addCleanup(use_bottle_root(self.stage / "root"))

    def _plan(self) -> FirecrackerBottlePlan:
        return FirecrackerBottlePlan(**_stage_fields(self.stage))

    def test_default_empty(self) -> None:
        self.assertEqual(self._plan().minted_cred_values, {})

    def test_minted_values_appear_in_egress_secrets(self) -> None:
        base = self._plan()
        plan = dataclasses.replace(
            base,
            minted_cred_values={"__cred__mylogin": "live-token"},
            egress_plan=dataclasses.replace(
                base.egress_plan,
                token_env_map={"EGRESS_TOKEN_0": "__cred__mylogin"}),
        )
        self.assertEqual(plan.egress_secrets.get("__cred__mylogin"), "live-token")

    def test_minted_values_win_over_provisioned_env_on_key_clash(self) -> None:
        # Both keys are merged into EgressTokenSecrets' provisioned layer.
        # minted_cred_values is merged AFTER provisioned_env, so it wins.
        # In practice these namespaces don't clash, but this documents precedence.
        fields = _stage_fields(self.stage)
        fields["agent_provision"] = dataclasses.replace(
            fields["agent_provision"], provisioned_env={"HOST_CRED": "from-provider"})
        plan = FirecrackerBottlePlan(**fields)
        plan_with = dataclasses.replace(plan, minted_cred_values={"HOST_CRED": "from-mint"})
        self.assertEqual(plan_with.egress_secrets.get("HOST_CRED"), "from-mint")

    def test_minted_values_not_in_repr(self) -> None:
        plan = dataclasses.replace(self._plan(), minted_cred_values={"tok": "SECRET"})
        self.assertNotIn("SECRET", repr(plan))


# ── forges.py credentials_raw allows new format ───────────────────────────────

class TestForgesCredentialsRaw(unittest.TestCase):

    def test_new_format_allowed(self) -> None:
        from bot_bottle.manifest.forges import credentials_raw
        credentials_raw("b", {"credentials": {"my-cred": {"source": "host_login"}}})

    def test_empty_block_allowed(self) -> None:
        from bot_bottle.manifest.forges import credentials_raw
        credentials_raw("b", {"credentials": {}})

    def test_absent_allowed(self) -> None:
        from bot_bottle.manifest.forges import credentials_raw
        credentials_raw("b", {})

    def test_old_format_rejected(self) -> None:
        from bot_bottle.manifest.forges import credentials_raw
        from bot_bottle.manifest.util import ManifestError
        with self.assertRaises(ManifestError):
            credentials_raw("b", {"credentials": {"github.com": {"token": "secret:X"}}})

    def test_full_index_with_credentials_block(self) -> None:
        # The full from_json_obj path now accepts the new credentials: block.
        idx = ManifestIndex.from_json_obj({
            "bottles": {"dev": {"credentials": {
                "my-login": {"source": "host_login", "harness": "codex"},
            }}},
            "agents": {"demo": {"skills": [], "prompt": ""}},
        })
        manifest = idx.load_for_agent("demo", ("dev",))
        self.assertIn("my-login", manifest.bottle.credentials)
        entry = manifest.bottle.credentials["my-login"]
        self.assertEqual(entry.source, "host_login")



# ── recredential_block / audit_cred_entries ───────────────────────────────────

_REC = "bottle-rec-1"


def _record(*entries: AdoptedCredEntry) -> AdoptionRecord:
    return AdoptionRecord(container="c", registry_id="reg-1",
                          cred_block=AdoptedCredBlock(entries) if entries else None)


def _entry(label: str = "tok", source: str = _FAKE_SOURCE) -> AdoptedCredEntry:
    return AdoptedCredEntry(label=label, source=source, kind="token",
                            settings={"harness": "claude"},
                            slots={"token": ["EGRESS_TOKEN_0", "EGRESS_TOKEN_1"]})


class TestRecredentialBlock(unittest.TestCase):
    """A restarted host controller re-mints an adopted bottle's credentials,
    pushes the values into their egress slots and re-registers them."""

    def _run(self, record: AdoptionRecord | None, *providers: CredentialProvider,
             attach: Any = None) -> tuple[mock.Mock, mock.Mock]:
        from bot_bottle.control.hostctl.cred_mint import recredential_block
        backend = mock.Mock()
        if isinstance(attach, Exception):
            backend.attach_adopted.side_effect = attach
        else:
            backend.attach_adopted.return_value = attach if attach is not None else mock.Mock()
        renewer, push = mock.Mock(), mock.Mock()
        with mock.patch("bot_bottle.backend.adoption.read", return_value=record), \
             mock.patch("bot_bottle.credentials.default_registry",
                        return_value=_registry_with(*providers)), \
             mock.patch("bot_bottle.secret_source.FileSecrets", _no_file_secrets), \
             mock.patch("bot_bottle.control.hostctl.credential_refresh.read_env_var_secret",
                        return_value="evsec"):
            recredential_block(mock.Mock(id=_REC), backend, renewer, push,
                               secrets_path=Path("/nonexistent"))
        return renewer, push

    def test_remints_pushes_each_slot_and_registers(self) -> None:
        renewer, push = self._run(_record(_entry()), _FakeMintProvider(token="fresh"))
        self.assertEqual(push.call_args_list, [
            mock.call("reg-1", "EGRESS_TOKEN_0", "fresh", "evsec"),
            mock.call("reg-1", "EGRESS_TOKEN_1", "fresh", "evsec"),
        ])
        renewer.add.assert_called_once()
        bottle_id, registry_id, evsec, entries = renewer.add.call_args.args
        self.assertEqual((bottle_id, registry_id, evsec), (_REC, "reg-1", "evsec"))
        label, minted, _, settings, _, slots = entries[0]
        self.assertEqual(label, "tok")
        self.assertEqual(minted.values["token"], "fresh")
        self.assertEqual(settings, {"harness": "claude"})
        self.assertEqual(slots, {"token": ("EGRESS_TOKEN_0", "EGRESS_TOKEN_1")})

    def test_nothing_adopted_is_a_no_op(self) -> None:
        for record in (None, _record()):
            renewer, push = self._run(record, _FakeMintProvider())
            push.assert_not_called()
            renewer.add.assert_not_called()

    def test_unknown_source_is_skipped(self) -> None:
        renewer, push = self._run(_record(_entry(source="nope")), _FakeMintProvider())
        push.assert_not_called()
        renewer.add.assert_not_called()

    def test_mint_failure_is_skipped(self) -> None:
        renewer, _ = self._run(_record(_entry()), _FailingProvider())
        renewer.add.assert_not_called()

    def test_attach_failure_gives_up(self) -> None:
        renewer, push = self._run(_record(_entry()), _FakeMintProvider(),
                                  attach=RuntimeError("gone"))
        push.assert_not_called()
        renewer.add.assert_not_called()

    def test_unattachable_bottle_gives_up(self) -> None:
        from bot_bottle.control.hostctl.cred_mint import recredential_block
        backend = mock.Mock()
        backend.attach_adopted.return_value = None
        renewer, push = mock.Mock(), mock.Mock()
        with mock.patch("bot_bottle.backend.adoption.read", return_value=_record(_entry())):
            recredential_block(mock.Mock(id=_REC), backend, renewer, push)
        push.assert_not_called()
        renewer.add.assert_not_called()


class TestAuditCredEntries(unittest.TestCase):
    def test_one_minted_event_per_entry(self) -> None:
        from bot_bottle.control.hostctl.cred_mint import audit_cred_entries
        prov = _FakeMintProvider()
        minted = MintedCredential(KIND_TOKEN, {"token": "t"}, EXPIRES, "fake:tok")
        audit = mock.Mock()
        audit_cred_entries([("tok", minted, prov, {}, MappingSecrets({}), {})],
                           audit, _REC, "act-1")
        audit.credential_event.assert_called_once_with(
            "credential.minted", bottle=_REC, activation="act-1",
            harness="credentials", label="tok", cred_kind=KIND_TOKEN,
            source=_FAKE_SOURCE, principal="fake:tok")


# ── $label resolution in settings ─────────────────────────────────────────────

class _TrackingProvider(CredentialProvider):
    """Records the settings each label is minted with."""
    source = _FAKE_SOURCE
    kinds = (KIND_TOKEN,)
    project_definable = True

    def __init__(self) -> None:
        self.received: dict[str, dict[str, object]] = {}

    def mint(self, label: str, bottle_id: str,
             settings: Mapping[str, object], secrets: object,
             *, minted_creds: Mapping[str, MintedCredential] | None = None,
             ) -> MintedCredential:
        self.received[label] = dict(settings)
        tok = "first-token" if label == "first" else "second-tok"
        return MintedCredential(KIND_TOKEN, {"token": tok}, None, label)

    def describe(self, settings: Mapping[str, object]) -> Grant:
        return Grant("", "", "", "", "tracking")


class TestLabelResolution(unittest.TestCase):

    def setUp(self) -> None:
        import tempfile, shutil
        self.stage = Path(tempfile.mkdtemp(prefix="label-test."))
        self.addCleanup(shutil.rmtree, self.stage, ignore_errors=True)
        self.addCleanup(use_bottle_root(self.stage / "root"))

    def _mint(self, credentials: dict[str, Any], prov: CredentialProvider) -> None:
        from bot_bottle.control.hostctl.cred_mint import mint_cred_block
        plan = FirecrackerBottlePlan(**_stage_fields(self.stage, {"credentials": credentials}))
        with mock.patch("bot_bottle.secret_source.FileSecrets", _no_file_secrets):
            mint_cred_block(plan, _SLUG, MappingSecrets({"TOK": "secret-tok"}),
                            registry=_registry_with(prov))

    def test_dollar_label_in_settings_resolved(self) -> None:
        """A later credential's `@first` setting is the minted token of `first`
        by the time it is minted. (`harness` is the one setting host_login
        accepts, so the manifest takes it.)"""
        prov = _TrackingProvider()
        self._mint({
            "first": {"source": _FAKE_SOURCE},
            "second": {"source": _FAKE_SOURCE, "harness": "@first"},
        }, prov)
        self.assertEqual(prov.received["second"].get("harness"), "first-token")

    def test_dollar_label_resolves_a_secret_credential(self) -> None:
        prov = _TrackingProvider()
        self._mint({
            "api": {"source": "secret", "secret": "TOK"},
            "second": {"source": _FAKE_SOURCE, "harness": "@api"},
        }, prov)
        self.assertEqual(prov.received["second"].get("harness"), "secret-tok")

    def test_double_at_setting_is_a_literal_at(self) -> None:
        prov = _TrackingProvider()
        self._mint({"first": {"source": _FAKE_SOURCE, "harness": "@@claude"}}, prov)
        self.assertEqual(prov.received["first"].get("harness"), "@claude")


class TestAgentCanRead(unittest.TestCase):
    """`agent_can_read: true` puts the minted values in `readable_cred_values`."""

    def setUp(self) -> None:
        import tempfile, shutil
        self.stage = Path(tempfile.mkdtemp(prefix="readable-test."))
        self.addCleanup(shutil.rmtree, self.stage, ignore_errors=True)
        self.addCleanup(use_bottle_root(self.stage / "root"))

    def _mint(self, cred: dict[str, Any]) -> FirecrackerBottlePlan:
        from bot_bottle.control.hostctl.cred_mint import mint_cred_block
        plan = FirecrackerBottlePlan(**_stage_fields(self.stage, {"credentials": {"mylogin": cred}}))
        plan = dataclasses.replace(plan, egress_plan=dataclasses.replace(
            plan.egress_plan, token_env_map={"EGRESS_TOKEN_0": "__cred__mylogin"}))
        with mock.patch("bot_bottle.secret_source.FileSecrets", _no_file_secrets):
            updated, _ = mint_cred_block(plan, _SLUG, MappingSecrets({}),
                                         registry=_registry_with(_FakeMintProvider(token="tok")))
        assert isinstance(updated, FirecrackerBottlePlan)
        return updated

    def test_agent_can_read_populates_readable_cred_values(self) -> None:
        updated = self._mint({"source": _FAKE_SOURCE, "agent_can_read": True})
        self.assertEqual(updated.readable_cred_values["mylogin"].get("token"), "tok")

    def test_agent_can_read_false_not_in_readable(self) -> None:
        updated = self._mint({"source": _FAKE_SOURCE})
        self.assertNotIn("mylogin", updated.readable_cred_values)


if __name__ == "__main__":
    unittest.main()
