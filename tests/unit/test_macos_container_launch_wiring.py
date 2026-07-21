"""Unit: macOS agent run wiring — the attribution invariant + token delivery
(PRD 0070).

Replaces the argv coverage from the per-bottle companion-container era
(`test_macos_container_launch.py`, removed with that architecture in #385).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import ANY, patch

from bot_bottle.backend.macos_container.bottle import MacosContainerBottle
from bot_bottle.backend.macos_container.bottle_plan import MacosContainerBottlePlan
from bot_bottle.backend.macos_container.consolidated_launch import GatewayEndpoint
from bot_bottle.backend.macos_container.gateway_hosts import GATEWAY_HOSTNAME
from bot_bottle.backend.macos_container.launch import (
    _agent_run_argv,
    _identity_proxy_env,
    build_or_load_images,
)
from bot_bottle.log import Die
from bot_bottle.manifest import ManifestIndex

_BOTTLE = "bot_bottle.backend.macos_container.bottle"

_MANIFEST = ManifestIndex.from_json_obj({
    "bottles": {"dev": {}},
    "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
}).load_for_agent("demo")


def _endpoint() -> GatewayEndpoint:
    return GatewayEndpoint(
        orchestrator_url="http://192.168.128.2:8099",
        gateway_ip="192.168.128.3",
        gateway_ca_pem="PEM",
        network="bot-bottle-mac-gateway",
    )


def _plan(
    stage_dir: Path,
    *,
    agent_git_gate_url: str = "",
    agent_supervise_url: str = "",
    image_policy: str = "fresh",
) -> MacosContainerBottlePlan:
    routes_path = stage_dir / "routes.yaml"
    routes_path.write_text("routes: []\n", encoding="utf-8")
    ca_path = stage_dir / "gateway-ca.pem"
    ca_path.write_text("ca\n", encoding="utf-8")
    egress_plan = SimpleNamespace(
        mitmproxy_ca_host_path=ca_path,
        routes_path=routes_path,
        routes=("route",),
        token_env_map={"EGRESS_TOKEN_0": "HOST_TOKEN"},
        canary="",
        canary_env="",
    )
    return cast(MacosContainerBottlePlan, SimpleNamespace(
        spec=SimpleNamespace(image_policy=image_policy),
        manifest=_MANIFEST,
        stage_dir=stage_dir,
        slug="dev-abc",
        container_name="bot-bottle-dev-abc",
        image="bot-bottle-agent:latest",
        dockerfile_path="/repo/Dockerfile",
        forwarded_env={"OAUTH_TOKEN": "host-value"},
        egress_plan=egress_plan,
        git_gate_plan=SimpleNamespace(upstreams=()),
        supervise_plan=None,
        agent_provision=SimpleNamespace(
            guest_env={"LITERAL": "value"},
            provisioned_env={},
        ),
        agent_git_gate_url=agent_git_gate_url,
        agent_supervise_url=agent_supervise_url,
    ))


class TestBuildOrLoadImages(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.plan = _plan(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reuses_present_committed_image(self) -> None:
        with (
            patch(
                "bot_bottle.backend.macos_container.launch.read_committed_image",
                return_value="committed:latest",
            ),
            patch(
                "bot_bottle.backend.macos_container.launch.container_mod.image_exists",
                return_value=True,
            ),
            patch(
                "bot_bottle.backend.macos_container.launch.container_mod.build_image"
            ) as build,
        ):
            images = build_or_load_images(self.plan)

        self.assertEqual("committed:latest", images.agent)
        build.assert_not_called()

    def test_reuses_present_cached_image(self) -> None:
        plan = _plan(Path(self._tmp.name), image_policy="cached")
        with (
            patch(
                "bot_bottle.backend.macos_container.launch.read_committed_image",
                return_value=None,
            ),
            patch(
                "bot_bottle.backend.macos_container.launch.container_mod.image_exists",
                return_value=True,
            ),
            patch(
                "bot_bottle.backend.macos_container.launch.container_mod.build_image"
            ) as build,
        ):
            images = build_or_load_images(plan)

        self.assertEqual(plan.image, images.agent)
        build.assert_not_called()

    def test_cached_policy_rejects_missing_image(self) -> None:
        plan = _plan(Path(self._tmp.name), image_policy="cached")
        with (
            patch(
                "bot_bottle.backend.macos_container.launch.read_committed_image",
                return_value=None,
            ),
            patch(
                "bot_bottle.backend.macos_container.launch.container_mod.image_exists",
                return_value=False,
            ),
        ):
            with self.assertRaises(Die):
                build_or_load_images(plan)

    def test_fresh_policy_builds_image(self) -> None:
        with (
            patch(
                "bot_bottle.backend.macos_container.launch.read_committed_image",
                return_value=None,
            ),
            patch(
                "bot_bottle.backend.macos_container.launch.container_mod.build_image"
            ) as build,
        ):
            images = build_or_load_images(self.plan)

        self.assertEqual(self.plan.image, images.agent)
        build.assert_called_once_with(
            self.plan.image,
            ANY,
            dockerfile=self.plan.dockerfile_path,
        )


class TestAgentRunArgv(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.argv = _agent_run_argv(_plan(Path(self._tmp.name)), _endpoint())

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_drops_net_raw(self) -> None:
        """The attribution invariant: Apple grants CAP_NET_RAW by default,
        which would let an agent forge a neighbour's source address with a raw
        socket and be attributed as that bottle."""
        self.assertIn("--cap-drop", self.argv)
        self.assertEqual("CAP_NET_RAW", self.argv[self.argv.index("--cap-drop") + 1])

    def test_attaches_to_the_shared_gateway_network(self) -> None:
        self.assertEqual(
            "bot-bottle-mac-gateway", self.argv[self.argv.index("--network") + 1],
        )

    def test_never_pins_an_ip(self) -> None:
        """Apple Container 1.0.0 has no --ip: the address is DHCP-assigned and
        read back after start."""
        self.assertNotIn("--ip", self.argv)

    def test_run_time_env_sets_no_proxy_vars_at_all(self) -> None:
        """Regression: the proxy vars must exist *only* in the exec-time env.

        `container exec --env` appends rather than replaces, so a token-less
        `HTTPS_PROXY` here would survive next to the token-bearing one and
        leave two entries in the agent's `environ`. Resolution is then
        runtime-specific — Node reads the last, Rust's `std::env::var` reads
        the first — so Codex proxied without its identity token and every
        request fail-closed at `/resolve`.
        """
        for entry in self.argv:
            self.assertFalse(
                entry.upper().startswith(("HTTP_PROXY=", "HTTPS_PROXY=")),
                f"run-time env must not set a proxy var, got {entry!r}",
            )

    def test_run_time_env_carries_no_identity_token(self) -> None:
        """The token is minted by registration, which happens after this run,
        so it cannot be here under any name."""
        self.assertNotIn("bottle:", " ".join(self.argv))

    def test_gateway_bypasses_the_proxy(self) -> None:
        """git-http + supervise live on the gateway and must be reached
        directly, not through its own egress proxy."""
        entry = next(a for a in self.argv if a.startswith("NO_PROXY="))
        self.assertIn(GATEWAY_HOSTNAME, entry)

    def test_no_proxy_names_the_gateway_and_never_addresses_it(self) -> None:
        """NO_PROXY is baked into the run-time env, so an address here is as
        unfixable as the proxy URL if the gateway moves."""
        entry = next(a for a in self.argv if a.startswith("NO_PROXY="))
        self.assertNotIn("192.168.128.3", entry)

    def test_forwarded_secrets_stay_off_argv(self) -> None:
        """Bare name → inherited from the run process env, so the value never
        lands on the command line."""
        self.assertIn("OAUTH_TOKEN", self.argv)
        self.assertNotIn("host-value", " ".join(self.argv))

    def test_agent_init_is_a_no_op(self) -> None:
        """Every agent command arrives via `container exec`; the init process
        just holds the container open."""
        self.assertEqual("sleep", self.argv[-2])


class TestIdentityTokenDelivery(unittest.TestCase):
    def test_exec_env_carries_the_token_as_proxy_credentials(self) -> None:
        env = _identity_proxy_env(_endpoint(), "s3cret")
        self.assertEqual(
            f"http://bottle:s3cret@{GATEWAY_HOSTNAME}:9099", env["HTTP_PROXY"],
        )
        self.assertEqual(env["HTTP_PROXY"], env["https_proxy"])

    def test_no_token_means_no_override(self) -> None:
        self.assertEqual({}, _identity_proxy_env(_endpoint(), ""))

    def test_token_value_never_reaches_argv(self) -> None:
        """`ps` is world-readable: the token rides the child env behind a bare
        `--env` name, never the command line."""
        bottle = MacosContainerBottle(
            "bot-bottle-demo", lambda: None, None,
            exec_env=_identity_proxy_env(_endpoint(), "s3cret"),
        )
        argv = bottle.agent_argv(["--help"], tty=False)
        self.assertNotIn("s3cret", " ".join(argv))
        self.assertIn("HTTP_PROXY", argv)
        self.assertEqual("--env", argv[argv.index("HTTP_PROXY") - 1])

    def test_bottle_without_exec_env_is_unchanged(self) -> None:
        bottle = MacosContainerBottle("bot-bottle-demo", lambda: None, None)
        argv = bottle.agent_argv(["--help"], tty=False)
        self.assertNotIn("--env", argv)


class TestPlanIdentityToken(unittest.TestCase):
    """git-gate's gitconfig extraHeader and the supervise MCP --header read
    `getattr(plan, "identity_token", "")` at provision time and both bypass the
    egress proxy (NO_PROXY), so the exec-time proxy token never reaches them —
    the plan must carry the token or /resolve fail-closes and git + supervise
    are denied on macOS."""

    def test_macos_plan_has_the_identity_token_field(self) -> None:
        from dataclasses import fields

        from bot_bottle.backend.macos_container.bottle_plan import (
            MacosContainerBottlePlan,
        )
        names = {f.name for f in fields(MacosContainerBottlePlan)}
        self.assertIn("identity_token", names)

    def test_matches_the_docker_plan(self) -> None:
        """Both consolidated backends must expose identity_token so a shared
        provision-time consumer degrades to neither backend silently."""
        from dataclasses import fields

        from bot_bottle.backend.docker.bottle_plan import DockerBottlePlan
        from bot_bottle.backend.macos_container.bottle_plan import (
            MacosContainerBottlePlan,
        )
        docker = {f.name for f in fields(DockerBottlePlan)}
        macos = {f.name for f in fields(MacosContainerBottlePlan)}
        self.assertIn("identity_token", docker & macos)

    def test_provisioning_exec_also_carries_the_token(self) -> None:
        """`provision` runs through `exec`; a provider whose provision step
        fetches anything would otherwise egress token-less and be denied."""
        bottle = MacosContainerBottle(
            "bot-bottle-demo", lambda: None, None,
            exec_env=_identity_proxy_env(_endpoint(), "s3cret"),
        )
        with patch(f"{_BOTTLE}.subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            bottle.exec("echo hi")
        argv, kwargs = run.call_args.args[0], run.call_args.kwargs
        self.assertIn("HTTP_PROXY", argv)
        self.assertNotIn("s3cret", " ".join(argv))
        self.assertEqual(
            f"http://bottle:s3cret@{GATEWAY_HOSTNAME}:9099", kwargs["env"]["HTTP_PROXY"],
        )


if __name__ == "__main__":
    unittest.main()
