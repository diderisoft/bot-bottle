"""Unit coverage for the fail-closed guest-local container engine (#392)."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from bot_bottle.backend.macos_container import nested_containers
from bot_bottle.backend.macos_container import launch as launch_mod
from bot_bottle.backend.macos_container.bottle_plan import MacosContainerBottlePlan


class _Bottle:
    def __init__(self, results: list[SimpleNamespace]) -> None:
        self.results = results
        self.commands: list[str] = []

    def exec(self, command: str) -> SimpleNamespace:
        self.commands.append(command)
        return self.results.pop(0)


def _result(returncode: int, *, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@dataclass(frozen=True)
class _AgentProvision:
    image: str


@dataclass(frozen=True)
class _Spec:
    image_policy: str = "build"


@dataclass(frozen=True)
class _Plan:
    slug: str
    image: str
    dockerfile_path: str
    nested_containers: bool
    agent_provision: _AgentProvision
    spec: _Spec = _Spec()


def _base_image_only(ref: str) -> bool:
    """Only the un-derived agent image is cached on the host."""
    return not ref.endswith(nested_containers.IMAGE_SUFFIX)


def _plan(**kwargs: object) -> MacosContainerBottlePlan:
    return cast(MacosContainerBottlePlan, cast(object, _Plan(**kwargs)))  # type: ignore[arg-type]


class TestNestedContainersStart(unittest.TestCase):
    def test_bootstraps_then_waits_for_guest_local_service(self) -> None:
        bottle = _Bottle([_result(0), _result(1), _result(0)])
        with patch.object(nested_containers.time, "sleep"):
            nested_containers.start(bottle)
        self.assertIn("nested-containers-init", bottle.commands[0])
        self.assertEqual(2, bottle.commands.count("docker info >/dev/null 2>&1"))

    def test_bootstrap_failure_is_fatal_without_privilege_fallback(self) -> None:
        bottle = _Bottle([_result(1, stderr="slirp4netns missing")])
        with patch.object(nested_containers, "die", side_effect=RuntimeError) as die:
            with self.assertRaises(RuntimeError):
                nested_containers.start(bottle)
        self.assertIn("slirp4netns missing", die.call_args.args[0])
        self.assertEqual(1, len(bottle.commands))

    def test_timeout_reports_guest_log(self) -> None:
        bottle = _Bottle(
            [_result(0)]
            + [_result(1) for _ in range(nested_containers.READY_RETRIES)]
            + [_result(0, stdout="operation not permitted")]
        )
        with patch.object(nested_containers.time, "sleep"), \
             patch.object(nested_containers, "die", side_effect=RuntimeError) as die:
            with self.assertRaises(RuntimeError):
                nested_containers.start(bottle)
        self.assertIn("operation not permitted", die.call_args.args[0])


class TestNestedContainersDevices(unittest.TestCase):
    def test_relaxes_only_the_two_blocked_device_nodes_as_root(self) -> None:
        calls: list[tuple[str, list[str]]] = []

        def record(name: str, argv: list[str]) -> None:
            calls.append((name, argv))

        nested_containers.prepare_guest_devices("bottle-1", record)
        self.assertEqual(1, len(calls))
        name, argv = calls[0]
        self.assertEqual("bottle-1", name)
        self.assertIn("chmod 0666 /dev/fuse /dev/net/tun", argv[-1])


class TestNestedContainersImage(unittest.TestCase):
    def test_layers_tooling_without_changing_base_image(self) -> None:
        calls: list[tuple[str, str, str]] = []

        def build(image: str, context: str, *, dockerfile: str) -> None:
            calls.append((image, context, dockerfile))
            text = Path(dockerfile).read_text(encoding="utf-8")
            self.assertIn("FROM agent:base", text)
            self.assertIn("aardvark-dns fuse-overlayfs netavark nftables passt", text)
            self.assertIn("USER node", text)
            self.assertTrue((Path(context) / "nested-containers-init.sh").is_file())

        image = nested_containers.build_image("agent:base", build)
        self.assertEqual("agent:base-nested-containers", image)
        self.assertEqual("agent:base-nested-containers", calls[0][0])

    def test_installs_the_whole_podman_5_networking_stack(self) -> None:
        """Each missing piece fails at a different, misleading layer: no pasta
        and nothing starts; no nft and netavark cannot build the bridge that
        compose expects; no aardvark-dns and DNS inside nested containers
        fails while everything else looks healthy. Image pulls keep working
        throughout, which is what made these read as compat-API bugs."""
        seen: list[str] = []

        def build(_image: str, _context: str, *, dockerfile: str) -> None:
            seen.append(Path(dockerfile).read_text(encoding="utf-8"))

        nested_containers.build_image("agent:base", build)
        for package in ("passt", "nftables", "aardvark-dns"):
            self.assertIn(package, seen[0])

    def test_strips_subordinate_ranges_so_podman_avoids_newuidmap(self) -> None:
        """The single-UID fallback is the entire reason podman works here.

        A subordinate range would send podman down the newuidmap path, which
        cannot write a multi-range uid_map without CAP_SYS_ADMIN in an Apple
        Container guest — the failure that killed the rootless-Docker spike.
        """
        seen: list[str] = []

        def build(_image: str, _context: str, *, dockerfile: str) -> None:
            seen.append(Path(dockerfile).read_text(encoding="utf-8"))

        nested_containers.build_image("agent:base", build)
        text = seen[0]
        self.assertIn("sed -i '/^node:/d' /etc/subuid /etc/subgid", text)
        self.assertNotIn("subuid", text.replace(
            "sed -i '/^node:/d' /etc/subuid /etc/subgid", "",
        ))


class TestInitScript(unittest.TestCase):
    """The bootstrap runs inside the bottle, so its wiring is only checkable
    here or on a live macOS host."""

    def setUp(self) -> None:
        self.script = (
            Path(nested_containers.__file__).with_name("nested-containers-init.sh")
            .read_text(encoding="utf-8")
        )

    def test_ca_bundle_path_matches_the_backend_constant(self) -> None:
        from bot_bottle.backend.util import AGENT_CA_BUNDLE
        self.assertIn(f'CA_BUNDLE="{AGENT_CA_BUNDLE}"', self.script)

    def test_resolves_the_gateway_address_for_nested_containers(self) -> None:
        """podman's hosts_file only applies to native `podman run` — the
        Docker-compat API ignores it, and the agent types `docker`. So the
        gateway name is resolved here and the *address* goes into the proxy
        URL; otherwise every nested container dies at "Could not resolve
        proxy", which reads like broken DNS but is a missing hosts entry."""
        self.assertIn('$2 == name { print $1; exit }', self.script)
        self.assertIn('value.replace(name, ip)', self.script)

    def test_docker_cli_proxy_config_uses_the_address(self) -> None:
        """The Docker CLI copies ~/.docker/config.json's proxies block into
        every container it starts. Being client-side it beats the podman
        service, so this — not containers.conf — is what decides whether a
        nested container can reach the proxy."""
        block = self.script[self.script.index('"proxies"'):]
        self.assertIn('"httpProxy": proxy.replace(name, ip)', block)
        self.assertIn('"httpsProxy": proxy.replace(name, ip)', block)
        self.assertIn('"noProxy": no_proxy', block)

    def test_substitutes_the_address_into_the_service_environment(self) -> None:
        """Covers what the Docker CLI does not stamp: podman's own registry
        pulls, and containers created through the API by another client."""
        launch = self.script[self.script.index("podman system service"):]
        self.assertNotIn("$GATEWAY_NAME", launch)  # substitution precedes it
        setup = self.script[:self.script.index("podman system service")]
        self.assertIn("for var in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy",
                      setup)

    def test_disables_podmans_own_proxy_passthrough(self) -> None:
        """podman copies the host's proxy vars into every container by
        default, and that copy overrides the env we set — putting the
        unresolvable gateway name back and leaving nested containers at
        "bad address 'bot-bottle-gateway'"."""
        self.assertIn('"http_proxy=false"', self.script)

    def test_keeps_the_gateway_name_in_no_proxy(self) -> None:
        """NO_PROXY is matched against what a client asks for, and code inside
        a nested container still says bot-bottle-gateway."""
        start = self.script.index('for var in ("NO_PROXY"')
        loop = self.script[start:self.script.index("path = Path(", start)]
        self.assertIn('entries.append(f"{var}={value}")', loop)
        self.assertNotIn("replace(name, ip)", loop)
        self.assertIn('"noProxy": no_proxy', self.script)

    def test_fails_closed_without_a_gateway_address(self) -> None:
        self.assertIn('[ -n "$gateway_ip" ] || {', self.script)

    def test_mounts_and_trusts_the_gateway_ca(self) -> None:
        """The gateway TLS-intercepts, so without the bundle every HTTPS call
        from a nested container fails with "unable to get local issuer
        certificate"."""
        self.assertIn('volumes=["{ca}:{ca}:ro"]', self.script)
        for var in (
            "SSL_CERT_FILE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE",
            "NODE_EXTRA_CA_CERTS",
        ):
            self.assertIn(f'f"{var}={{ca}}"', self.script)

    def test_writes_the_token_bearing_config_unreadable_to_others(self) -> None:
        """The proxy URL carries the bottle's identity token."""
        self.assertIn("path.chmod(0o600)", self.script)


class TestGuestEnvironment(unittest.TestCase):
    def test_disabled_bottle_gets_no_docker_environment(self) -> None:
        self.assertEqual({}, nested_containers.guest_env(False))

    def test_enabled_bottle_uses_only_the_guest_local_socket(self) -> None:
        env = nested_containers.guest_env(True)
        self.assertEqual("unix:///tmp/bbp/podman.sock", env["DOCKER_HOST"])
        self.assertNotIn("/var/run/docker.sock", " ".join(env.values()))

    def test_runtime_dir_leaves_room_for_conmons_attach_socket(self) -> None:
        """podman builds `$XDG_RUNTIME_DIR/libpod/tmp/socket/<64-hex>/attach`,
        which must fit in sun_path (108 bytes). A descriptive runtime dir blew
        past it and every attached `docker run` failed with "unable to upgrade
        to tcp, received 500" while pulls and detached runs looked fine."""
        attach = (
            nested_containers.guest_env(True)["XDG_RUNTIME_DIR"]
            + "/libpod/tmp/socket/" + "a" * 64 + "/attach"
        )
        self.assertLessEqual(len(attach), 107, attach)

    def test_macos_backend_declares_support(self) -> None:
        from bot_bottle.backend.macos_container.backend import (
            MacosContainerBottleBackend,
        )
        self.assertTrue(MacosContainerBottleBackend.supports_nested_containers)


class TestBuildOrLoadImages(unittest.TestCase):
    def test_disabled_bottle_keeps_the_plain_agent_image(self) -> None:
        plan = _plan(
            slug="dev-abc", image="agent:base", dockerfile_path="/repo/Dockerfile",
            nested_containers=False, agent_provision=_AgentProvision("agent:base"),
        )
        with patch.object(launch_mod, "read_committed_image", return_value=None), \
             patch.object(launch_mod.container_mod, "build_image"), \
             patch.object(launch_mod.nested_containers_mod, "build_image") as derived:
            images = launch_mod.build_or_load_images(plan)
        derived.assert_not_called()
        self.assertEqual("agent:base", images.agent)

    def test_enabled_bottle_builds_base_then_derived_variant(self) -> None:
        plan = _plan(
            slug="dev-abc", image="agent:base", dockerfile_path="/repo/Dockerfile",
            nested_containers=True, agent_provision=_AgentProvision("agent:base"),
        )
        with patch.object(launch_mod, "read_committed_image", return_value=None), \
             patch.object(launch_mod.container_mod, "build_image") as build, \
             patch.object(
                 launch_mod.nested_containers_mod,
                 "build_image",
                 return_value="agent:base-nested-containers",
             ) as derived:
            images = launch_mod.build_or_load_images(plan)

        build.assert_called_once_with(
            "agent:base", launch_mod._REPO_DIR,  # pylint: disable=protected-access
            dockerfile="/repo/Dockerfile",
        )
        derived.assert_called_once_with("agent:base", build)
        self.assertEqual("agent:base-nested-containers", images.agent)

    def test_derived_image_layers_onto_a_committed_image(self) -> None:
        plan = _plan(
            slug="dev-abc", image="agent:base", dockerfile_path="/repo/Dockerfile",
            nested_containers=True, agent_provision=_AgentProvision("agent:base"),
        )
        with patch.object(
                 launch_mod, "read_committed_image", return_value="agent:committed",
             ), \
             patch.object(launch_mod.container_mod, "image_exists", return_value=True), \
             patch.object(launch_mod.container_mod, "build_image") as build, \
             patch.object(
                 launch_mod.nested_containers_mod,
                 "build_image",
                 return_value="agent:committed-nested-containers",
             ) as derived:
            images = launch_mod.build_or_load_images(plan)

        build.assert_not_called()
        derived.assert_called_once_with("agent:committed", build)
        self.assertEqual("agent:committed-nested-containers", images.agent)

    def test_cached_policy_refuses_to_build_the_derived_image(self) -> None:
        plan = _plan(
            slug="dev-abc", image="agent:base", dockerfile_path="/repo/Dockerfile",
            nested_containers=True, agent_provision=_AgentProvision("agent:base"),
            spec=_Spec(image_policy="cached"),
        )
        with patch.object(launch_mod, "read_committed_image", return_value=None), \
             patch.object(
                 launch_mod.container_mod, "image_exists",
                 side_effect=_base_image_only,
             ), \
             patch.object(launch_mod.nested_containers_mod, "build_image") as derived, \
             patch.object(launch_mod, "die", side_effect=RuntimeError) as die:
            with self.assertRaises(RuntimeError):
                launch_mod.build_or_load_images(plan)
        derived.assert_not_called()
        self.assertIn("agent:base-nested-containers", die.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
