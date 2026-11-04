"""A bottle's `resources:` (memory and vCPUs): parsing, merging through
`extends` and stacking, the bottle document, each backend's launch arguments,
and the host budget counting each bottle at its own size."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bot_bottle.backend import bottle_size
from bot_bottle.backend.bottle_size import BOTTLE_MEMORY_BYTES, BOTTLE_MEMORY_MIB
from bot_bottle.backend.docker.consolidated_compose import consolidated_agent_compose
from bot_bottle.backend.macos_container.launch import _agent_run_argv  # pyright: ignore[reportPrivateUsage]
from bot_bottle.control.hostctl import budget
from bot_bottle.control.hostctl.budget import HostBudget
from bot_bottle.control.hostctl.manifest_elements import (
    _drop, _file_elements, _set,  # pyright: ignore[reportPrivateUsage]
)
from bot_bottle.manifest import Manifest
from bot_bottle.manifest.agent import ManifestAgent
from bot_bottle.manifest.bottle import ManifestBottle
from bot_bottle.manifest.document import bottle_document
from bot_bottle.manifest.extends import merge_bottles_runtime, resolve_bottles
from bot_bottle.manifest.fingerprint import bottle_hash
from bot_bottle.manifest.preview import manifest_to_yaml
from bot_bottle.manifest.resources import (
    CPUS_MAX, MEMORY_MAX_MIB, MEMORY_MIN_MIB, ManifestResources,
)
from bot_bottle.manifest.util import ManifestError
from tests.unit._docker_bottle_plan import _plan as _docker_plan
from tests.unit.test_macos_container_launch_wiring import _endpoint, _plan as _macos_plan


def _bottle(raw: dict[str, object]) -> ManifestBottle:
    return ManifestBottle.from_dict("b", raw)


def _with_resources(plan: Any, resources: ManifestResources) -> Any:
    bottle = dataclasses.replace(plan.manifest.bottle, resources=resources)
    manifest = dataclasses.replace(plan.manifest, bottle=bottle)
    if isinstance(plan, SimpleNamespace):  # the macOS tests' stand-in plan
        return SimpleNamespace(**{**vars(plan), "manifest": manifest})
    return dataclasses.replace(plan, manifest=manifest)


class TestParse(unittest.TestCase):
    def test_unset_is_the_backends_size(self) -> None:
        bottle = _bottle({})
        self.assertEqual(ManifestResources(), bottle.resources)
        self.assertEqual(BOTTLE_MEMORY_MIB, bottle_size.memory_mib(bottle))
        self.assertIsNone(bottle_size.cpus(bottle))

    def test_memory_takes_mib_or_a_unit(self) -> None:
        for raw, mib in ((4096, 4096), ("4G", 4096), ("4GiB", 4096), ("512M", 512),
                         ("1536MiB", 1536), (" 2 g ", 2048)):
            with self.subTest(raw=raw):
                self.assertEqual(mib, _bottle({"resources": {"memory": raw}}).resources.memory_mib)

    def test_cpus_is_a_whole_number(self) -> None:
        self.assertEqual(4, _bottle({"resources": {"cpus": 4}}).resources.cpus)

    def test_refuses_nonsense(self) -> None:
        for raw in ({"memory": "lots"}, {"memory": True}, {"memory": 4.5}, {"memory": "4T"},
                    {"memory": MEMORY_MIN_MIB - 1}, {"memory": MEMORY_MAX_MIB + 1},
                    {"cpus": 0}, {"cpus": CPUS_MAX + 1}, {"cpus": "2"}, {"cpus": True},
                    {"cpus": 1.5}, {"disk": 10}):
            with self.subTest(raw=raw), self.assertRaises(ManifestError):
                _bottle({"resources": raw})

    def test_refuses_a_non_mapping(self) -> None:
        with self.assertRaises(ManifestError):
            _bottle({"resources": "big"})


class TestMerge(unittest.TestCase):
    def test_extends_replaces_per_key(self) -> None:
        resolved = resolve_bottles({
            "base": {"resources": {"memory": "4G", "cpus": 2}},
            "child": {"extends": "base", "resources": {"cpus": 8}},
            "plain": {"extends": "base"},
        })
        self.assertEqual(ManifestResources(memory_mib=4096, cpus=8), resolved["child"].resources)
        self.assertEqual(ManifestResources(memory_mib=4096, cpus=2), resolved["plain"].resources)

    def test_multiple_parents_fold_left_to_right(self) -> None:
        resolved = resolve_bottles({
            "a": {"resources": {"memory": "4G"}},
            "b": {"resources": {"memory": "8G", "cpus": 3}},
            "c": {"extends": ["a", "b"]},
        })
        self.assertEqual(ManifestResources(memory_mib=8192, cpus=3), resolved["c"].resources)

    def test_stacking_later_wins_per_key(self) -> None:
        merged = merge_bottles_runtime([
            _bottle({"resources": {"memory": "4G", "cpus": 2}}),
            _bottle({"resources": {"memory": "1G"}}),
        ])
        self.assertEqual(ManifestResources(memory_mib=1024, cpus=2), merged.resources)


class TestDocument(unittest.TestCase):
    def test_round_trips(self) -> None:
        bottle = _bottle({"resources": {"memory": "4G", "cpus": 4}})
        doc = bottle_document(bottle)
        self.assertEqual({"memory": 4096, "cpus": 4}, doc["resources"])
        self.assertEqual(bottle.resources, _bottle(doc).resources)

    def test_unset_leaves_documents_and_fingerprints_alone(self) -> None:
        self.assertNotIn("resources", bottle_document(_bottle({})))
        self.assertEqual(bottle_hash(_bottle({})),
                         bottle_hash(_bottle({"resources": {}})))

    def test_preview_shows_it(self) -> None:
        manifest = Manifest(agent=ManifestAgent(), bottle=_bottle({"resources": {"cpus": 4}}))
        self.assertIn("  resources:\n    cpus: 4", manifest_to_yaml(manifest))
        self.assertNotIn("resources:", manifest_to_yaml(Manifest(agent=ManifestAgent(),
                                                                 bottle=_bottle({}))))


class TestBackends(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(self._tmp.cleanup)

    def test_docker_takes_memory_and_cpus(self) -> None:
        plan = _with_resources(_docker_plan(), ManifestResources(memory_mib=4096, cpus=3))
        agent = consolidated_agent_compose(plan, gateway_ip="10.0.0.1", source_ip="10.0.0.2",
                                           network="net")["services"]["agent"]
        self.assertEqual("4096m", agent["mem_limit"])
        self.assertEqual(3.0, agent["cpus"])

    def test_docker_keeps_its_default_cpus_when_unset(self) -> None:
        agent = consolidated_agent_compose(_docker_plan(), gateway_ip="10.0.0.1",
                                           source_ip="10.0.0.2",
                                           network="net")["services"]["agent"]
        self.assertNotIn("cpus", agent)

    def test_macos_takes_memory_and_cpus(self) -> None:
        plan = _with_resources(_macos_plan(Path(self._tmp.name)),
                               ManifestResources(memory_mib=3072, cpus=6))
        argv = _agent_run_argv(plan, _endpoint())
        self.assertEqual("3072M", argv[argv.index("--memory") + 1])
        self.assertEqual("6", argv[argv.index("--cpus") + 1])

    def test_macos_keeps_its_default_cpus_when_unset(self) -> None:
        self.assertNotIn("--cpus", _agent_run_argv(_macos_plan(Path(self._tmp.name)),
                                                   _endpoint()))


class TestBudget(unittest.TestCase):
    def test_refuses_by_the_launching_bottles_own_size(self) -> None:
        limits = HostBudget(memory_gb=8)
        running = [{"id": "a", "memory": 4 * 1024 ** 3}]
        self.assertIsNone(budget.refusal("small", running, limits, None, 2 * 1024 ** 3))
        refused = budget.refusal("big", running, limits, None, 6 * 1024 ** 3)
        assert refused is not None
        self.assertIn("big needs 6 GB of memory", refused)

    def test_recorded_memory_falls_back_to_the_fixed_size(self) -> None:
        self.assertEqual(BOTTLE_MEMORY_BYTES, bottle_size.recorded_memory({}))
        self.assertEqual(BOTTLE_MEMORY_BYTES, bottle_size.recorded_memory({"memory": "x"}))
        self.assertEqual(4096, bottle_size.recorded_memory({"memory": 4096}))
        self.assertEqual(4 * 1024 ** 3, bottle_size.memory_bytes(
            _bottle({"resources": {"memory": "4G"}})))


class TestElements(unittest.TestCase):
    def test_listed_set_and_dropped_like_env(self) -> None:
        fm: dict[str, Any] = {"resources": {"memory": "4G"}}
        self.assertEqual([("resources:memory", "memory", "4G", "4G")], _file_elements(fm))
        _set(fm, "resources:cpus", 4, {})
        self.assertEqual({"memory": "4G", "cpus": 4}, fm["resources"])
        _drop(fm, "resources:memory")
        self.assertEqual({"cpus": 4}, fm["resources"])


if __name__ == "__main__":
    unittest.main()
