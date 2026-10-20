"""Unit: the host `Settings` contract (PRD 0092) and the CLI's env loader."""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot_bottle.cli import settings as loader
from bot_bottle.cli.settings import INFRA_ENV, OPERATION_ENV, settings_from_env
from bot_bottle.contract.hostctl import InfraSettings, OperationSettings, Settings
from tests.unit import use_bottle_root


def _custom() -> Settings:
    return Settings(
        operation=OperationSettings(backend="docker", no_cache=True,
                                    cleanup_command_timeout_seconds=7.5),
        infra=InfraSettings(infra_build_local=True, egress_port=9199, fc_pool_size=3,
                            gateway_image="gw:test", macos_container_dns="9.9.9.9"),
    )


class TestSettingsJson(unittest.TestCase):
    def test_round_trips(self) -> None:
        for settings in (Settings(), _custom()):
            with self.subTest(settings=settings):
                wire = json.loads(json.dumps(settings.to_json()))
                self.assertEqual(settings, Settings.from_json(wire))

    def test_to_json_lists_every_field_by_kind(self) -> None:
        body = Settings().to_json()
        self.assertEqual({"operation", "infra"}, set(body))
        self.assertEqual({f.name for f in dataclasses.fields(OperationSettings)},
                         set(body["operation"]))
        self.assertEqual({f.name for f in dataclasses.fields(InfraSettings)}, set(body["infra"]))

    def test_missing_keys_take_defaults(self) -> None:
        self.assertEqual(Settings(), Settings.from_json({}))
        partial = Settings.from_json({"operation": {"no_cache": True}})
        self.assertTrue(partial.operation.no_cache)
        self.assertEqual(InfraSettings(), partial.infra)

    def test_int_is_accepted_for_a_float(self) -> None:
        parsed = Settings.from_json({"operation": {"cleanup_command_timeout_seconds": 30}})
        self.assertEqual(30.0, parsed.operation.cleanup_command_timeout_seconds)
        self.assertIs(float, type(parsed.operation.cleanup_command_timeout_seconds))

    def test_rejects_malformed_input(self) -> None:
        cases = {
            "not an object": [],
            "unknown top-level key": {"extra": {}},
            "unknown operation key": {"operation": {"surprise": 1}},
            "unknown infra key": {"infra": {"BOT_BOTTLE_EGRESS_PORT": 1}},
            "section not an object": {"infra": "x"},
            "str for bool": {"operation": {"no_cache": "1"}},
            "int for bool": {"operation": {"no_cache": 1}},
            "bool for int": {"infra": {"egress_port": True}},
            "str for int": {"infra": {"egress_port": "9099"}},
            "int for str": {"infra": {"gateway_image": 3}},
            "float for int": {"infra": {"fc_pool_size": 2.0}},
            "negative int": {"infra": {"fc_pool_size": -1}},
            "negative float": {"operation": {"cleanup_command_timeout_seconds": -1.0}},
        }
        for name, data in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                Settings.from_json(data)

    def test_settings_are_immutable(self) -> None:
        with self.assertRaises(dataclasses.FrozenInstanceError):
            Settings().operation.no_cache = True  # type: ignore[misc]


class TestFingerprint(unittest.TestCase):
    def test_stable_and_sensitive(self) -> None:
        self.assertEqual(InfraSettings().fingerprint(), InfraSettings().fingerprint())
        self.assertEqual(64, len(InfraSettings().fingerprint()))
        self.assertNotEqual(InfraSettings().fingerprint(),
                            InfraSettings(egress_port=9100).fingerprint())

    def test_known_default_value(self) -> None:
        # Pinned so an accidental change to the canonical form is noticed; a
        # deliberate field change updates this.
        self.assertEqual(
            "4955111b08908f4605de0d3788b6aeb684f6626775cf1f4a512f31d3aa496b59",
            InfraSettings().fingerprint(),
        )

    def test_ignores_operation_settings(self) -> None:
        a = Settings(operation=OperationSettings(no_cache=True))
        self.assertEqual(Settings().infra.fingerprint(), a.infra.fingerprint())


class TestEnvLoader(unittest.TestCase):
    def test_env_names_cover_every_field(self) -> None:
        self.assertEqual({f.name for f in dataclasses.fields(OperationSettings)},
                         set(OPERATION_ENV))
        self.assertEqual({f.name for f in dataclasses.fields(InfraSettings)}, set(INFRA_ENV))
        names = [*OPERATION_ENV.values(), *INFRA_ENV.values()]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(n.startswith("BOT_BOTTLE_") for n in names))

    def test_defaults_when_unset_or_empty(self) -> None:
        self.assertEqual(Settings(), settings_from_env({}))
        blank = {name: "  " for name in [*OPERATION_ENV.values(), *INFRA_ENV.values()]}
        self.assertEqual(Settings(), settings_from_env(blank))

    def test_reads_the_process_environment_by_default(self) -> None:
        with patch.dict("os.environ", {"BOT_BOTTLE_EGRESS_PORT": "9123"}):
            self.assertEqual(9123, settings_from_env().infra.egress_port)

    def test_every_field(self) -> None:
        env = {
            "BOT_BOTTLE_BACKEND": "firecracker",
            "BOT_BOTTLE_NO_CACHE": "1",
            "BOT_BOTTLE_CLEANUP_COMMAND_TIMEOUT_SECONDS": "30",
            "BOT_BOTTLE_INFRA_BUILD": "LOCAL",
            "BOT_BOTTLE_BUILD_ENGINE": "Podman",
            "BOT_BOTTLE_ARTIFACT_CACHE": "/cache/artifacts",
            "BOT_BOTTLE_ORCHESTRATOR_IMAGE": "orch:test",
            "BOT_BOTTLE_GATEWAY_IMAGE": "gw:test",
            "BOT_BOTTLE_EGRESS_PORT": "9199",
            "BOT_BOTTLE_DOCKER_GATEWAY_SUBNET": "10.1.2.0/24",
            "BOT_BOTTLE_DOCKER_CA_MOUNT": "ca-volume",
            "BOT_BOTTLE_DOCKER_ROOT_MOUNT": "root-volume",
            "BOT_BOTTLE_DOCKER_CLIENT_NETWORK": "ci-net",
            "BOT_BOTTLE_DOCKER_HOST_ADDRESS": "172.17.0.1",
            "BOT_BOTTLE_FC_KERNEL": "/fc/vmlinux",
            "BOT_BOTTLE_FC_DROPBEAR": "/fc/dropbear",
            "BOT_BOTTLE_FC_POOL_SIZE": "4",
            "BOT_BOTTLE_FC_IP_BASE": "10.9.0.0",
            "BOT_BOTTLE_FC_IFACE_PREFIX": "bbci",
            "BOT_BOTTLE_FC_NFT_TABLE": "bb_ci",
            "BOT_BOTTLE_FC_ORCH_IFACE": "bbciorch0",
            "BOT_BOTTLE_FC_GW_IFACE": "bbcigw0",
            "BOT_BOTTLE_MACOS_CONTAINER_DNS": "9.9.9.9",
        }
        expected = Settings(
            operation=OperationSettings(
                backend="firecracker", no_cache=True, cleanup_command_timeout_seconds=30.0),
            infra=InfraSettings(
                infra_build_local=True, build_engine="podman", artifact_cache="/cache/artifacts",
                orchestrator_image="orch:test", gateway_image="gw:test", egress_port=9199,
                docker_gateway_subnet="10.1.2.0/24", docker_ca_mount="ca-volume",
                docker_root_mount="root-volume", docker_client_network="ci-net",
                docker_host_address="172.17.0.1", fc_kernel="/fc/vmlinux",
                fc_dropbear="/fc/dropbear", fc_pool_size=4, fc_ip_base="10.9.0.0",
                fc_iface_prefix="bbci", fc_nft_table="bb_ci", fc_orch_iface="bbciorch0",
                fc_gw_iface="bbcigw0", macos_container_dns="9.9.9.9",
            ),
        )
        self.assertEqual(expected, settings_from_env(env))
        # Every field moved off its default, so none was silently dropped.
        defaults = Settings()
        for section in ("operation", "infra"):
            got, default = getattr(expected, section), getattr(defaults, section)
            for f in dataclasses.fields(got):
                with self.subTest(field=f.name):
                    self.assertNotEqual(getattr(default, f.name), getattr(got, f.name))

    def test_flag_values(self) -> None:
        self.assertFalse(settings_from_env({"BOT_BOTTLE_NO_CACHE": "true"}).operation.no_cache)
        self.assertFalse(
            settings_from_env({"BOT_BOTTLE_INFRA_BUILD": "pull"}).infra.infra_build_local)

    def test_malformed_timeout_falls_back_to_the_default(self) -> None:
        got = settings_from_env({"BOT_BOTTLE_CLEANUP_COMMAND_TIMEOUT_SECONDS": "soon"})
        self.assertEqual(Settings().operation, got.operation)

    def test_malformed_integer_dies_naming_the_var(self) -> None:
        for raw in ("abc", "-1"):
            with self.subTest(raw=raw), \
                 patch.object(loader, "die", side_effect=SystemExit) as die, \
                 self.assertRaises(SystemExit):
                settings_from_env({"BOT_BOTTLE_EGRESS_PORT": raw})
            self.assertIn("BOT_BOTTLE_EGRESS_PORT", die.call_args.args[0])


class TestInfraSettingsRecord(unittest.TestCase):
    def test_bring_up_records_the_fingerprint_only_when_it_restarted(self) -> None:
        from bot_bottle.backend.docker.infra import DockerInfraService

        settings = _custom()
        with tempfile.TemporaryDirectory() as root:
            self.addCleanup(use_bottle_root(Path(root)))
            svc = DockerInfraService(settings)
            self.assertIsNone(svc.recorded_infra_fingerprint())
            with patch.object(DockerInfraService, "_bring_up", return_value=("http://x", False)):
                svc.ensure_running()
            self.assertIsNone(svc.recorded_infra_fingerprint())
            with patch.object(DockerInfraService, "_bring_up", return_value=("http://x", True)), \
                 patch("bot_bottle.backend.infra_service.reattach_running_bottles"):
                svc.ensure_running()
            self.assertEqual(settings.infra.fingerprint(), svc.recorded_infra_fingerprint())


if __name__ == "__main__":
    unittest.main()
