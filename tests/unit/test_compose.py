"""Unit: compose-spec renderer (PRD 0018 chunk 1).

Pure-function tests for `bottle_plan_to_compose`. Fixtures build a
fully-resolved DockerBottlePlan in memory; the renderer just
translates it to the compose dict. Conditional-service matrix is
covered via parameterized cases (git on/off × egress on/off ×
supervise on/off).
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot_bottle.agent_provider import AgentProvisionPlan
from bot_bottle.backend import BottleSpec
from bot_bottle.backend.docker.bottle_plan import DockerBottlePlan
from bot_bottle.backend.docker.compose import (
    COMPOSE_PROJECT_PREFIX,
    bottle_plan_to_compose,
    compose_project_name,
    list_active_slugs,
    list_compose_projects,
    slug_from_compose_project,
)
from bot_bottle.egress import (
    EgressPlan,
    EgressRoute,
)
from bot_bottle.git_gate import GitGatePlan, GitGateUpstream
from bot_bottle.manifest import Manifest
from bot_bottle.pipelock import PipelockProxyPlan
from bot_bottle.supervise import SupervisePlan
from bot_bottle.workspace import workspace_plan


SLUG = "demo-abc12"
STAGE = Path("/tmp/cb-stage")
STATE = Path("/tmp/cb-state")


def _manifest(*, supervise: bool, with_git: bool, with_egress: bool) -> Manifest:
    """Minimal manifest with the toggles the chunk-1 matrix needs.
    The renderer only reads from the plan, not the manifest, so this
    is just here to back BottleSpec."""
    bottle: dict[str, object] = {}
    if supervise:
        bottle["supervise"] = True
    if with_git:
        bottle["git-gate"] = {"repos": {
            "upstream": {
                "url": "ssh://git@example.com:22/x/y.git",
                "identity": "/etc/hostname",  # any existing file
            },
        }}
    if with_egress:
        bottle["egress"] = {
            "routes": [{
                "host": "api.example",
                "auth": {"scheme": "Bearer", "token_ref": "TOK"},
            }],
        }
    return Manifest.from_json_obj({
        "bottles": {"dev": bottle},
        "agents": {"demo": {"skills": [], "prompt": "", "bottle": "dev"}},
    })


def _spec(*, supervise: bool, with_git: bool, with_egress: bool) -> BottleSpec:
    return BottleSpec(
        manifest=_manifest(
            supervise=supervise, with_git=with_git, with_egress=with_egress,
        ),
        agent_name="demo",
        copy_cwd=False,
        user_cwd="/tmp/x",
    )


def _proxy_plan() -> PipelockProxyPlan:
    return PipelockProxyPlan(
        yaml_path=STATE / "pipelock.yaml",
        slug=SLUG,
        internal_network=f"bot-bottle-net-{SLUG}",
        internal_network_cidr="10.1.2.0/24",
        egress_network=f"bot-bottle-egress-{SLUG}",
        ca_cert_host_path=STATE / "pipelock-ca" / "ca.pem",
        ca_key_host_path=STATE / "pipelock-ca" / "ca-key.pem",
    )


def _git_gate_plan(upstreams: tuple[GitGateUpstream, ...] = ()) -> GitGatePlan:
    return GitGatePlan(
        slug=SLUG,
        entrypoint_script=STATE / "git-gate" / "entrypoint.sh",
        hook_script=STATE / "git-gate" / "pre-receive",
        access_hook_script=STATE / "git-gate" / "access-hook",
        upstreams=upstreams,
        internal_network=f"bot-bottle-net-{SLUG}",
        egress_network=f"bot-bottle-egress-{SLUG}",
    )


def _egress_plan(routes: tuple[EgressRoute, ...] = ()) -> EgressPlan:
    token_env_map = {
        r.token_env: r.token_ref
        for r in routes
        if r.token_env
    }
    return EgressPlan(
        slug=SLUG,
        routes_path=STATE / "egress" / "routes.yaml",
        routes=routes,
        token_env_map=token_env_map,
        internal_network=f"bot-bottle-net-{SLUG}",
        egress_network=f"bot-bottle-egress-{SLUG}",
        mitmproxy_ca_host_path=STATE / "egress-ca" / "mitmproxy-ca.pem",
        mitmproxy_ca_cert_only_host_path=STATE / "egress-ca" / "ca.pem",
        pipelock_ca_host_path=STATE / "pipelock-ca" / "ca.pem",
        pipelock_proxy_url="http://127.0.0.1:8888",
    )


def _supervise_plan() -> SupervisePlan:
    return SupervisePlan(
        slug=SLUG,
        queue_dir=STATE / "supervise" / "queue",
        current_config_dir=STATE / "supervise" / "current-config",
        internal_network=f"bot-bottle-net-{SLUG}",
    )


def _plan(
    *,
    with_git: bool = False,
    with_egress: bool = False,
    supervise: bool = False,
) -> DockerBottlePlan:
    """Build a fully-resolved DockerBottlePlan. Toggles cover the
    matrix the renderer's conditional-service logic branches on."""
    upstreams: tuple[GitGateUpstream, ...] = ()
    if with_git:
        upstreams = (GitGateUpstream(
            name="upstream",
            upstream_url="ssh://git@example.com:22/x/y.git",
            upstream_host="example.com",
            upstream_port="22",
            identity_file="/etc/hostname",
            known_host_key="",
            known_hosts_file=STATE / "git-gate" / "upstream-known_hosts",
        ),)
    routes: tuple[EgressRoute, ...] = ()
    if with_egress:
        routes = (EgressRoute(
            host="api.example",
            auth_scheme="Bearer",
            token_env="EGRESS_TOKEN_0",
            token_ref="TOK",
            path_allowlist=(),
            roles=(),
        ),)

    spec = _spec(supervise=supervise, with_git=with_git, with_egress=with_egress)
    return DockerBottlePlan(
        guest_home="/home/node",
        spec=spec,
        stage_dir=STAGE,
        slug=SLUG,
        container_name=f"bot-bottle-{SLUG}",
        container_name_pinned=False,
        image="bot-bottle-claude:latest",
        derived_image="",
        runtime_image="bot-bottle-claude:latest",
        dockerfile_path="",
        env_file=Path("/dev/null"),  # exists, size 0 → renderer skips env_file
        forwarded_env={"CLAUDE_CODE_OAUTH_TOKEN": "x"},
        prompt_file=STAGE / "prompt",
        proxy_plan=_proxy_plan(),
        git_gate_plan=_git_gate_plan(upstreams),
        egress_plan=_egress_plan(routes),
        supervise_plan=_supervise_plan() if supervise else None,
        use_runsc=False,
        agent_provision=AgentProvisionPlan(
            template="claude",
            command="claude",
            prompt_mode="append_file",
            image="bot-bottle-claude:latest",
            dockerfile="",
            guest_env={},
        ),
        workspace_plan=workspace_plan(spec, guest_home="/home/node"),
    )


class TestProjectAndNetworks(unittest.TestCase):
    def test_project_name(self):
        spec = bottle_plan_to_compose(_plan())
        self.assertEqual(f"bot-bottle-{SLUG}", spec["name"])

    def test_internal_network_is_internal(self):
        spec = bottle_plan_to_compose(_plan())
        net = spec["networks"]["internal"]
        self.assertEqual(f"bot-bottle-net-{SLUG}", net["name"])
        self.assertTrue(net["internal"])

    def test_egress_network_is_external_bridge(self):
        spec = bottle_plan_to_compose(_plan())
        net = spec["networks"]["egress"]
        self.assertEqual(f"bot-bottle-egress-{SLUG}", net["name"])
        # No `internal:` key on the egress network — defaults to a
        # normal user-defined bridge.
        self.assertNotIn("internal", net)


class TestAgentAlwaysPresent(unittest.TestCase):
    def test_agent_in_services(self):
        s = bottle_plan_to_compose(_plan())["services"]
        self.assertIn("agent", s)

    def test_agent_command(self):
        s = bottle_plan_to_compose(_plan())["services"]["agent"]
        self.assertEqual(["sleep", "infinity"], s["command"])

    def test_agent_image_uses_runtime_image(self):
        plan = _plan()
        s = bottle_plan_to_compose(plan)["services"]["agent"]
        self.assertEqual(plan.runtime_image, s["image"])

    def test_agent_only_on_internal_network(self):
        s = bottle_plan_to_compose(_plan())["services"]["agent"]
        self.assertEqual({"internal"}, set(s["networks"].keys()))

    def test_agent_proxy_via_pipelock_when_no_egress(self):
        s = bottle_plan_to_compose(_plan(with_egress=False))["services"]["agent"]
        env = s["environment"]
        # Looking for HTTPS_PROXY pointing at pipelock's container name.
        proxy_lines = [e for e in env if e.startswith("HTTPS_PROXY=")]
        self.assertEqual(1, len(proxy_lines))
        self.assertEqual(
            "HTTPS_PROXY=http://pipelock:8888",
            proxy_lines[0],
        )

    def test_agent_proxy_via_egress_when_egress_present(self):
        s = bottle_plan_to_compose(_plan(with_egress=True))["services"]["agent"]
        proxy = [e for e in s["environment"] if e.startswith("HTTPS_PROXY=")][0]
        self.assertEqual("HTTPS_PROXY=http://egress:9099", proxy)

    def test_agent_no_proxy_adds_supervise_when_enabled(self):
        s = bottle_plan_to_compose(
            _plan(supervise=True)
        )["services"]["agent"]
        no_proxy = [e for e in s["environment"] if e.startswith("NO_PROXY=")][0]
        self.assertIn("supervise", no_proxy)

    def test_agent_forwarded_env_uses_bare_names(self):
        # Bare NAME → compose inherits value from the up-process env,
        # so secret token values stay out of the file.
        s = bottle_plan_to_compose(_plan())["services"]["agent"]
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", s["environment"])

    def test_agent_provider_env_uses_literal_values(self):
        plan = _plan()
        provision = AgentProvisionPlan(
            template="codex",
            command="codex",
            prompt_mode="read_prompt_file",
            image="bot-bottle-codex:latest",
            dockerfile="",
            guest_env={"CODEX_HOME": "/home/node/.codex"},
        )
        plan = type(plan)(**{**vars(plan), "agent_provision": provision})  # type: ignore
        s = bottle_plan_to_compose(plan)["services"]["agent"]
        self.assertIn("CODEX_HOME=/home/node/.codex", s["environment"])

    def test_agent_runsc_runtime(self):
        plan = _plan()
        plan = type(plan)(**{**vars(plan), "use_runsc": True})  # type: ignore
        s = bottle_plan_to_compose(plan)["services"]["agent"]
        self.assertEqual("runsc", s["runtime"])

    def test_agent_depends_only_on_sidecars(self):
        # Bundle shape: the init supervisor owns intra-bundle daemon
        # ordering, so the agent waits on the bundle container alone.
        for kwargs in [{}, {"with_git": True, "with_egress": True, "supervise": True}]:
            with self.subTest(**kwargs):
                s = bottle_plan_to_compose(_plan(**kwargs))["services"]["agent"]
                self.assertEqual(["sidecars"], s["depends_on"])

    def test_agent_current_config_mount_only_with_supervise(self):
        with_sv = bottle_plan_to_compose(_plan(supervise=True))["services"]["agent"]
        self.assertTrue(any(
            v["target"] == "/etc/bot-bottle/current-config"
            for v in with_sv.get("volumes", [])
        ))
        without_sv = bottle_plan_to_compose(_plan(supervise=False))["services"]["agent"]
        # Either no volumes key at all, or no current-config target.
        self.assertFalse(any(
            v["target"] == "/etc/bot-bottle/current-config"
            for v in without_sv.get("volumes", [])
        ))


class TestSidecarBundleShape(unittest.TestCase):
    """The compose renderer emits exactly one `sidecars` service in
    place of the four daemons it owns (pipelock + egress + git-gate
    + supervise). PRD 0024 chunk 5 dropped the legacy four-sidecar
    shape entirely, so the bundle is the only thing exercised here."""

    def _render(self, **plan_kwargs: object) -> Any:  # type: ignore
        return bottle_plan_to_compose(_plan(**plan_kwargs))  # type: ignore

    def test_emits_two_services_minimal(self):
        spec = self._render()
        self.assertEqual({"sidecars", "agent"}, set(spec["services"].keys()))

    def test_emits_two_services_full_matrix(self):
        spec = self._render(with_git=True, with_egress=True, supervise=True)
        # Still two services — the bundle absorbs git-gate/egress/supervise.
        self.assertEqual({"sidecars", "agent"}, set(spec["services"].keys()))

    def test_bundle_uses_bundle_image_and_dockerfile(self):
        sc = self._render()["services"]["sidecars"]
        self.assertEqual("bot-bottle-sidecars:latest", sc["image"])
        self.assertEqual("Dockerfile.sidecars", sc["build"]["dockerfile"])

    def test_bundle_container_name_uses_sidecars_prefix(self):
        sc = self._render()["services"]["sidecars"]
        self.assertEqual(f"bot-bottle-sidecars-{SLUG}", sc["container_name"])

    def test_bundle_joins_both_networks(self):
        sc = self._render()["services"]["sidecars"]
        self.assertEqual({"internal", "egress"}, set(sc["networks"].keys()))

    def test_internal_aliases_cover_pipelock_and_egress_shortnames(self):
        # The agent's HTTPS_PROXY url references either `egress` or
        # `pipelock`. Both must resolve to the bundle.
        sc = self._render()["services"]["sidecars"]
        aliases = set(sc["networks"]["internal"]["aliases"])
        self.assertIn("egress", aliases)
        self.assertIn("pipelock", aliases)

    def test_internal_aliases_omit_inactive_sidecars(self):
        # With no git-gate / supervise, those names are NOT aliased
        # — keeps the alias list honest about what's actually
        # listening inside the bundle.
        sc = self._render()["services"]["sidecars"]
        aliases = set(sc["networks"]["internal"]["aliases"])
        self.assertNotIn("git-gate", aliases)
        self.assertNotIn("supervise", aliases)

    def test_internal_aliases_include_active_sidecars(self):
        sc = self._render(with_git=True, supervise=True)["services"]["sidecars"]
        aliases = set(sc["networks"]["internal"]["aliases"])
        self.assertIn("git-gate", aliases)
        self.assertIn("supervise", aliases)

    def test_daemons_csv_lists_only_active(self):
        # Egress + pipelock are always in the daemon set even when
        # the bottle has no routes (egress falls back to regular@9099
        # and is just unused; cheaper than special-casing).
        sc = self._render()["services"]["sidecars"]
        daemons = {
            line.split("=", 1)[1]
            for line in sc["environment"]
            if line.startswith("BOT_BOTTLE_SIDECAR_DAEMONS=")
        }
        self.assertEqual({"egress,pipelock"}, daemons)

    def test_daemons_csv_expands_with_optional_sidecars(self):
        sc = self._render(with_git=True, supervise=True)["services"]["sidecars"]
        for line in sc["environment"]:
            if line.startswith("BOT_BOTTLE_SIDECAR_DAEMONS="):
                csv = line.split("=", 1)[1]
                break
        else:
            self.fail("BOT_BOTTLE_SIDECAR_DAEMONS not in env")
        self.assertEqual(
            ["egress", "pipelock", "git-gate", "supervise"],
            csv.split(","),
        )

    def test_bundle_env_does_not_set_https_proxy(self):
        # HTTPS_PROXY at the container level would route git-gate's
        # git fetches through pipelock. Scoping it to mitmdump is
        # the job of egress_entrypoint.sh; the bundle env must not
        # leak it.
        sc = self._render(with_egress=True)["services"]["sidecars"]
        for line in sc["environment"]:
            self.assertFalse(
                line.startswith("HTTPS_PROXY=")
                or line.startswith("HTTP_PROXY=")
                or line.startswith("NO_PROXY="),
                f"bundle env must not set {line!r}",
            )

    def test_egress_env_present_when_routes_declared(self):
        sc = self._render(with_egress=True)["services"]["sidecars"]
        env_strings = sc["environment"]
        self.assertTrue(any(
            e.startswith("EGRESS_UPSTREAM_PROXY=") for e in env_strings))
        self.assertTrue(any(
            e.startswith("EGRESS_UPSTREAM_CA=") for e in env_strings))
        # Token env name is forwarded as a bare entry.
        self.assertIn("EGRESS_TOKEN_0", env_strings)

    def test_egress_env_omitted_when_no_routes(self):
        sc = self._render()["services"]["sidecars"]
        env_strings = sc["environment"]
        for e in env_strings:
            self.assertFalse(e.startswith("EGRESS_UPSTREAM_PROXY="))
            self.assertFalse(e.startswith("EGRESS_UPSTREAM_CA="))

    def test_supervise_env_present_when_active(self):
        sc = self._render(supervise=True)["services"]["sidecars"]
        env_strings = sc["environment"]
        self.assertIn(f"SUPERVISE_BOTTLE_SLUG={SLUG}", env_strings)
        self.assertTrue(any(e.startswith("SUPERVISE_QUEUE_DIR=") for e in env_strings))
        self.assertTrue(any(e.startswith("SUPERVISE_PORT=") for e in env_strings))

    def test_volumes_union_minimal_includes_pipelock(self):
        sc = self._render()["services"]["sidecars"]
        targets = {v["target"] for v in sc["volumes"]}
        self.assertIn("/etc/pipelock.yaml", targets)

    def test_volumes_union_full_matrix(self):
        sc = self._render(with_git=True, with_egress=True, supervise=True)[
            "services"]["sidecars"]
        targets = {v["target"] for v in sc["volumes"]}
        # Pipelock + egress + git-gate + supervise paths all
        # present.
        self.assertIn("/etc/pipelock.yaml", targets)
        self.assertIn("/etc/egress/routes.yaml", targets)
        self.assertIn("/git-gate-entrypoint.sh", targets)
        self.assertIn("/git-gate/creds/upstream-known_hosts", targets)
        # supervise queue dir target = QUEUE_DIR_IN_CONTAINER
        self.assertTrue(any("supervise/queue" in t or t.startswith("/run/supervise")
                            for t in targets))

    def test_extra_hosts_omitted_for_git_upstreams(self):
        sc = self._render(with_git=True)["services"]["sidecars"]
        self.assertNotIn("extra_hosts", sc)

    def test_agent_depends_on_bundle_only(self):
        sc = self._render(with_git=True, with_egress=True, supervise=True)[
            "services"]["agent"]
        self.assertEqual(["sidecars"], sc["depends_on"])

    def test_agent_proxy_url_resolves_via_bundle_alias(self):
        # With egress active, the agent's HTTPS_PROXY points at
        # `egress` shortname; bundle aliases `egress` to itself so
        # the URL keeps working without an agent-side change.
        spec = self._render(with_egress=True)
        sc = spec["services"]["agent"]
        proxy = next(e for e in sc["environment"] if e.startswith("HTTPS_PROXY="))
        self.assertIn("egress", proxy)
        self.assertIn("egress",
                      spec["services"]["sidecars"]["networks"]["internal"]["aliases"])


class TestProjectNaming(unittest.TestCase):
    """The slug ↔ compose-project mapping is the contract dashboard,
    cleanup, and launch all rely on. Lock it down."""

    def test_compose_project_name_is_prefix_plus_slug(self):
        self.assertEqual(
            f"{COMPOSE_PROJECT_PREFIX}myagent-abc12",
            compose_project_name("myagent-abc12"),
        )

    def test_slug_from_compose_project_is_inverse(self):
        self.assertEqual(
            "myagent-abc12",
            slug_from_compose_project(f"{COMPOSE_PROJECT_PREFIX}myagent-abc12"),
        )

    def test_slug_from_unrelated_project_returns_empty(self):
        # Defends against `docker compose ls` including non-bottle
        # projects on a host with other compose setups.
        self.assertEqual("", slug_from_compose_project("other-project"))


class TestComposeProjectListing(unittest.TestCase):
    def test_compose_ls_error_warns_by_default(self):
        with (
            mock.patch(
                "bot_bottle.backend.docker.compose.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["docker"], returncode=1, stdout="", stderr="no daemon",
                ),
            ),
            mock.patch("bot_bottle.backend.docker.compose.warn") as warn,
        ):
            self.assertEqual([], list_compose_projects())
        warn.assert_called_once_with("docker compose ls failed: no daemon")

    def test_compose_ls_error_can_be_quiet_for_dashboard_polling(self):
        with (
            mock.patch(
                "bot_bottle.backend.docker.compose.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["docker"], returncode=1, stdout="", stderr="no daemon",
                ),
            ),
            mock.patch("bot_bottle.backend.docker.compose.warn") as warn,
        ):
            self.assertEqual([], list_active_slugs(warn_on_error=False))
        warn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
