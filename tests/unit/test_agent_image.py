"""PRD 0085 consumer resolution: agent_image resolves/materializes a pinned
agent image, or returns None so the caller builds locally."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from bot_bottle import agent_image as ai
from bot_bottle import published_artifacts as pa

_DIGEST_OCI = "sha256:" + "a" * 64
_DIGEST_EXT4 = "sha256:" + "b" * 64


def _manifest(*, oci: bool = True, ext4: bool = True, plat: str = "linux/arm64"):
    on_disk = plat.replace("/", "-")
    artifacts: dict[str, object] = {}
    if oci:
        artifacts[f"oci_{on_disk}"] = {
            "type": "oci", "platform": on_disk,
            "url": f"gitea.dideric.is/didericis/bot-bottle-claude:0.14.2-{on_disk}",
            "digest": _DIGEST_OCI,
        }
    if ext4:
        artifacts[f"ext4_{on_disk}"] = {
            "type": "ext4", "platform": on_disk,
            "url": ("https://gitea.dideric.is/api/packages/didericis/generic/"
                    "bot-bottle-claude-rootfs/0.14.2/rootfs.arm64.ext4.gz"),
            "digest": _DIGEST_EXT4,
        }
    return pa.parse_artifacts_manifest({
        "bot-bottle-claude": {
            "version": "0.14.2", "inputs_hash": "9f2c1e4b7a83d10c", "protocol": 1,
            "artifacts": artifacts,
        }
    })


class AgentImageName(unittest.TestCase):
    def test_builtin_template_maps_to_published_name(self) -> None:
        self.assertEqual(ai.agent_image_name("claude"), "bot-bottle-claude")

    def test_custom_template_has_no_published_name(self) -> None:
        self.assertIsNone(ai.agent_image_name("my-custom-agent"))

    def test_infra_role_is_not_an_agent(self) -> None:
        self.assertIsNone(ai.agent_image_name("gateway"))


class ResolvePin(unittest.TestCase):
    def test_none_without_a_manifest(self) -> None:
        with mock.patch.object(ai, "load_artifacts_manifest_optional", return_value=None):
            self.assertIsNone(ai.resolve_agent_pin("claude", prefer=(pa.FORMAT_OCI,)))

    def test_none_for_a_custom_template(self) -> None:
        # Never even consults the manifest — a custom template has no default.
        with mock.patch.object(ai, "load_artifacts_manifest_optional") as load:
            self.assertIsNone(ai.resolve_agent_pin("mine", prefer=(pa.FORMAT_OCI,)))
            load.assert_not_called()

    def test_returns_the_pinned_oci_artifact(self) -> None:
        with mock.patch.object(ai, "load_artifacts_manifest_optional", return_value=_manifest()), \
                mock.patch.object(ai, "current_platform", return_value="linux/arm64"):
            art = ai.resolve_agent_pin("claude", prefer=(pa.FORMAT_OCI,))
        assert art is not None
        self.assertEqual(art.format, "oci")
        self.assertEqual(art.digest, _DIGEST_OCI)

    def test_none_when_the_preferred_format_is_missing(self) -> None:
        with mock.patch.object(ai, "load_artifacts_manifest_optional",
                               return_value=_manifest(ext4=False)), \
                mock.patch.object(ai, "current_platform", return_value="linux/arm64"):
            self.assertIsNone(ai.resolve_agent_pin("claude", prefer=(pa.FORMAT_EXT4,)))


def _manifest_pair(
    *, agent_version: str = "0.14.2", infra_version: str = "0.14.2",
    agent_protocol: int = 1, infra_protocol: int = 1, plat: str = "linux/arm64",
):
    on_disk = plat.replace("/", "-")

    def _entry(name: str, version: str, protocol: int):
        return {
            "version": version, "inputs_hash": "9f2c1e4b7a83d10c", "protocol": protocol,
            "artifacts": {
                f"oci_{on_disk}": {
                    "type": "oci", "platform": on_disk,
                    "url": f"gitea.dideric.is/didericis/{name}:{version}-{on_disk}",
                    "digest": _DIGEST_OCI,
                },
            },
        }

    return pa.parse_artifacts_manifest({
        "bot-bottle-claude": _entry("bot-bottle-claude", agent_version, agent_protocol),
        "bot-bottle-gateway": _entry("bot-bottle-gateway", infra_version, infra_protocol),
    })


class ProtocolCompat(unittest.TestCase):
    def _resolve(self, entries: dict[str, pa.ImageEntry]) -> pa.Artifact | None:
        with mock.patch.object(ai, "load_artifacts_manifest_optional", return_value=entries), \
                mock.patch.object(ai, "current_platform", return_value="linux/arm64"), \
                mock.patch.object(ai, "PROTOCOL_VERSION", 1):
            return ai.resolve_agent_pin("claude", prefer=(pa.FORMAT_OCI,))

    def test_matching_protocol_resolves(self) -> None:
        self.assertIsNotNone(self._resolve(_manifest_pair()))

    def test_partial_republish_at_a_new_version_still_resolves(self) -> None:
        # Republishing only the agent image bumps its version, not its protocol.
        entries = _manifest_pair(agent_version="0.14.3", infra_version="0.14.2")
        self.assertIsNotNone(self._resolve(entries))

    def test_agent_infra_protocol_mismatch_refuses(self) -> None:
        entries = _manifest_pair(agent_protocol=1, infra_protocol=2)
        with self.assertRaises(pa.ArtifactsManifestError):
            self._resolve(entries)

    def test_agent_protocol_unlike_this_host_refuses(self) -> None:
        entries = _manifest_pair(agent_protocol=2, infra_protocol=2)
        with self.assertRaises(pa.ArtifactsManifestError):
            self._resolve(entries)


class Materialize(unittest.TestCase):
    def test_ensure_oci_pulls_and_returns_pinned_ref(self) -> None:
        with mock.patch.object(ai, "load_artifacts_manifest_optional", return_value=_manifest()), \
                mock.patch.object(ai, "current_platform", return_value="linux/arm64"), \
                mock.patch.object(ai.image_registry, "resolve") as resolve, \
                mock.patch.object(ai.image_registry, "pull") as pull:
            resolve.return_value = mock.Mock(pinned_ref="reg/o/bot-bottle-claude@" + _DIGEST_OCI)
            ref = ai.ensure_agent_oci("claude", runtime="docker")
        self.assertEqual(ref, "reg/o/bot-bottle-claude@" + _DIGEST_OCI)
        pull.assert_called_once()

    def test_ensure_oci_none_without_a_pin_never_pulls(self) -> None:
        with mock.patch.object(ai, "load_artifacts_manifest_optional", return_value=None), \
                mock.patch.object(ai.image_registry, "pull") as pull:
            self.assertIsNone(ai.ensure_agent_oci("claude"))
            pull.assert_not_called()

    def test_ensure_ext4_returns_cached_path(self) -> None:
        with mock.patch.object(ai, "load_artifacts_manifest_optional", return_value=_manifest()), \
                mock.patch.object(ai, "current_platform", return_value="linux/arm64"), \
                mock.patch.object(ai.image_registry, "ensure_ext4",
                                  return_value=Path("/cache/x.ext4.gz")) as ensure:
            path = ai.ensure_agent_ext4("claude")
        self.assertEqual(path, Path("/cache/x.ext4.gz"))
        ensure.assert_called_once()

    def test_ensure_ext4_none_without_a_pin(self) -> None:
        with mock.patch.object(ai, "load_artifacts_manifest_optional", return_value=None), \
                mock.patch.object(ai.image_registry, "ensure_ext4") as ensure:
            self.assertIsNone(ai.ensure_agent_ext4("claude"))
            ensure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
