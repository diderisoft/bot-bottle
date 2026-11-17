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
    AdoptedCredBlock, AdoptedCredEntry, cred_block_for,
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


def _index(bottle_extra: dict | None = None) -> ManifestIndex:
    bottle_def: dict[str, object] = {}
    if bottle_extra:
        bottle_def.update(bottle_extra)
    return ManifestIndex.from_json_obj({
        "bottles": {"dev": bottle_def},
        "agents": {"demo": {"skills": [], "prompt": ""}},
    })


def _stage_fields(stage: Path, bottle_extra: dict | None = None) -> dict[str, Any]:
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
        self.mint_calls: list[tuple] = []

    def mint(self, label: str, bottle_id: str,
             settings: Mapping[str, object], secrets: object) -> MintedCredential:
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
             settings: Mapping[str, object], secrets: object) -> MintedCredential:
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

    def _plan(self, bottle_extra: dict | None = None,
              token_env_map: dict | None = None) -> FirecrackerBottlePlan:
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
        with mock.patch("bot_bottle.secret_source.FileSecrets", lambda _: MappingSecrets({})):
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
        with mock.patch("bot_bottle.secret_source.FileSecrets", lambda _: MappingSecrets({})):
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
        with mock.patch("bot_bottle.secret_source.FileSecrets", lambda _: MappingSecrets({})):
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
        with mock.patch("bot_bottle.secret_source.FileSecrets", lambda _: MappingSecrets({})):
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
        with mock.patch("bot_bottle.secret_source.FileSecrets", lambda _: MappingSecrets({})):
            _, entries = mint_cred_block(plan, _SLUG, MappingSecrets({}), registry=reg)
        slots = entries[0][5]
        self.assertIn("EGRESS_TOKEN_0", slots["token"])
        self.assertIn("EGRESS_TOKEN_1", slots["token"])
        self.assertNotIn("EGRESS_TOKEN_2", slots["token"])

    def test_no_matching_slot_still_mints(self) -> None:
        from bot_bottle.control.hostctl.cred_mint import mint_cred_block
        prov = _FakeMintProvider(token="env-only")
        reg = _registry_with(prov)
        plan = self._plan(
            bottle_extra={"credentials": {"env-cred": {"source": _FAKE_SOURCE}}},
            token_env_map={},  # no egress slots
        )
        with mock.patch("bot_bottle.secret_source.FileSecrets", lambda _: MappingSecrets({})):
            updated, entries = mint_cred_block(plan, _SLUG, MappingSecrets({}), registry=reg)
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

    def _plan(self, bottle_extra: dict | None = None,
              token_env_map: dict | None = None) -> FirecrackerBottlePlan:
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


if __name__ == "__main__":
    unittest.main()
