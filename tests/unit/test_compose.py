"""Unit: compose-spec renderer (PRD 0018 chunk 1).

Pure-function tests for `bottle_plan_to_compose`. Fixtures build a
fully-resolved DockerBottlePlan in memory; the renderer just
translates it to the compose dict. Conditional-service matrix is
covered via parameterized cases (git on/off × egress on/off ×
supervise on/off).
"""

from __future__ import annotations

import unittest
from pathlib import Path

from claude_bottle.backend import BottleSpec
from claude_bottle.backend.docker.bottle_plan import DockerBottlePlan
from claude_bottle.backend.docker.compose import bottle_plan_to_compose
from claude_bottle.egress import (
    EgressPlan,
    EgressRoute,
)
from claude_bottle.git_gate import GitGatePlan, GitGateUpstream
from claude_bottle.manifest import Manifest
from claude_bottle.pipelock import PipelockProxyPlan
from claude_bottle.supervise import SupervisePlan


SLUG = "demo-abc12"
STAGE = Path("/tmp/cb-stage")
STATE = Path("/tmp/cb-state")


def _manifest(*, supervise: bool, with_git: bool, with_egress: bool) -> Manifest:
    """Minimal manifest with the toggles the chunk-1 matrix needs.
    The renderer only reads from the plan, not the manifest, so this
    is just here to back BottleSpec."""
    bottle: dict = {}
    if supervise:
        bottle["supervise"] = True
    if with_git:
        bottle["git"] = [{
            "Name": "upstream",
            "Upstream": "ssh://git@example.com:22/x/y.git",
            "IdentityFile": "/etc/hostname",  # any existing file
        }]
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
        internal_network=f"claude-bottle-net-{SLUG}",
        internal_network_cidr="10.1.2.0/24",
        egress_network=f"claude-bottle-egress-{SLUG}",
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
        internal_network=f"claude-bottle-net-{SLUG}",
        egress_network=f"claude-bottle-egress-{SLUG}",
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
        internal_network=f"claude-bottle-net-{SLUG}",
        egress_network=f"claude-bottle-egress-{SLUG}",
        mitmproxy_ca_host_path=STATE / "egress-ca" / "mitmproxy-ca.pem",
        mitmproxy_ca_cert_only_host_path=STATE / "egress-ca" / "ca.pem",
        pipelock_ca_host_path=STATE / "pipelock-ca" / "ca.pem",
        pipelock_proxy_url=f"http://claude-bottle-pipelock-{SLUG}:8888",
    )


def _supervise_plan() -> SupervisePlan:
    return SupervisePlan(
        slug=SLUG,
        queue_dir=STATE / "supervise" / "queue",
        current_config_dir=STATE / "supervise" / "current-config",
        internal_network=f"claude-bottle-net-{SLUG}",
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
            extra_hosts={"example.com": "10.0.0.1"},
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

    return DockerBottlePlan(
        spec=_spec(supervise=supervise, with_git=with_git, with_egress=with_egress),
        stage_dir=STAGE,
        slug=SLUG,
        container_name=f"claude-bottle-{SLUG}",
        container_name_pinned=False,
        image="claude-bottle:latest",
        derived_image="",
        runtime_image="claude-bottle:latest",
        dockerfile_path="",
        env_file=Path("/dev/null"),  # exists, size 0 → renderer skips env_file
        forwarded_env={"CLAUDE_CODE_OAUTH_TOKEN": "x"},
        prompt_file=STAGE / "prompt",
        proxy_plan=_proxy_plan(),
        git_gate_plan=_git_gate_plan(upstreams),
        egress_plan=_egress_plan(routes),
        supervise_plan=_supervise_plan() if supervise else None,
        use_runsc=False,
    )


class TestProjectAndNetworks(unittest.TestCase):
    def test_project_name(self):
        spec = bottle_plan_to_compose(_plan())
        self.assertEqual(f"claude-bottle-{SLUG}", spec["name"])

    def test_internal_network_marked_external(self):
        # Chunk 3 pre-creates networks with `docker network create
        # --internal` so pipelock can know the CIDR before compose-up.
        # Compose references the network by name with `external: true`.
        spec = bottle_plan_to_compose(_plan())
        net = spec["networks"]["internal"]
        self.assertEqual(f"claude-bottle-net-{SLUG}", net["name"])
        self.assertTrue(net["external"])

    def test_egress_network_marked_external(self):
        spec = bottle_plan_to_compose(_plan())
        net = spec["networks"]["egress"]
        self.assertEqual(f"claude-bottle-egress-{SLUG}", net["name"])
        self.assertTrue(net["external"])


class TestPipelockAlwaysPresent(unittest.TestCase):
    """Pipelock is unconditional — every bottle has the SSRF guard +
    body scanner sitting on its upstream leg."""

    def test_minimal_plan_has_pipelock(self):
        spec = bottle_plan_to_compose(_plan())
        self.assertIn("pipelock", spec["services"])

    def test_pipelock_pinned_image_no_build(self):
        s = bottle_plan_to_compose(_plan())["services"]["pipelock"]
        self.assertTrue(s["image"].startswith("ghcr.io/luckypipewrench/pipelock"))
        self.assertNotIn("build", s)

    def test_pipelock_container_name(self):
        s = bottle_plan_to_compose(_plan())["services"]["pipelock"]
        self.assertEqual(f"claude-bottle-pipelock-{SLUG}", s["container_name"])

    def test_pipelock_on_both_networks(self):
        s = bottle_plan_to_compose(_plan())["services"]["pipelock"]
        self.assertIn("internal", s["networks"])
        self.assertIn("egress", s["networks"])

    def test_pipelock_long_name_alias_on_internal(self):
        # Backward compat: anything still dialing pipelock by
        # `claude-bottle-pipelock-<slug>` resolves on the internal
        # network.
        s = bottle_plan_to_compose(_plan())["services"]["pipelock"]
        aliases = s["networks"]["internal"]["aliases"]
        self.assertIn(f"claude-bottle-pipelock-{SLUG}", aliases)

    def test_pipelock_bind_mounts(self):
        s = bottle_plan_to_compose(_plan())["services"]["pipelock"]
        targets = {v["target"] for v in s["volumes"]}
        self.assertEqual(
            {"/etc/pipelock.yaml", "/etc/pipelock-ca.pem", "/etc/pipelock-ca-key.pem"},
            targets,
        )
        for v in s["volumes"]:
            self.assertEqual("bind", v["type"])
            self.assertTrue(v["read_only"])

    def test_pipelock_command(self):
        s = bottle_plan_to_compose(_plan())["services"]["pipelock"]
        self.assertEqual(
            ["run", "--config", "/etc/pipelock.yaml", "--listen", "0.0.0.0:8888"],
            s["command"],
        )


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
            f"HTTPS_PROXY=http://claude-bottle-pipelock-{SLUG}:8888",
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

    def test_agent_runsc_runtime(self):
        plan = _plan()
        plan = type(plan)(**{**vars(plan), "use_runsc": True})
        s = bottle_plan_to_compose(plan)["services"]["agent"]
        self.assertEqual("runsc", s["runtime"])

    def test_agent_depends_on_pipelock(self):
        s = bottle_plan_to_compose(_plan())["services"]["agent"]
        self.assertIn("pipelock", s["depends_on"])

    def test_agent_depends_on_every_present_sidecar(self):
        s = bottle_plan_to_compose(
            _plan(with_git=True, with_egress=True, supervise=True)
        )["services"]["agent"]
        self.assertEqual(
            {"pipelock", "git-gate", "egress", "supervise"},
            set(s["depends_on"]),
        )

    def test_agent_current_config_mount_only_with_supervise(self):
        with_sv = bottle_plan_to_compose(_plan(supervise=True))["services"]["agent"]
        self.assertTrue(any(
            v["target"] == "/etc/claude-bottle/current-config"
            for v in with_sv.get("volumes", [])
        ))
        without_sv = bottle_plan_to_compose(_plan(supervise=False))["services"]["agent"]
        # Either no volumes key at all, or no current-config target.
        self.assertFalse(any(
            v["target"] == "/etc/claude-bottle/current-config"
            for v in without_sv.get("volumes", [])
        ))


class TestConditionalGitGate(unittest.TestCase):
    def test_absent_when_no_upstreams(self):
        s = bottle_plan_to_compose(_plan(with_git=False))["services"]
        self.assertNotIn("git-gate", s)

    def test_present_when_upstreams(self):
        s = bottle_plan_to_compose(_plan(with_git=True))["services"]
        self.assertIn("git-gate", s)

    def test_git_gate_built_from_dockerfile(self):
        s = bottle_plan_to_compose(_plan(with_git=True))["services"]["git-gate"]
        self.assertEqual("Dockerfile.git-gate", s["build"]["dockerfile"])
        self.assertEqual("claude-bottle-git-gate:latest", s["image"])

    def test_git_gate_extra_hosts(self):
        s = bottle_plan_to_compose(_plan(with_git=True))["services"]["git-gate"]
        self.assertIn("example.com:10.0.0.1", s["extra_hosts"])

    def test_git_gate_identity_file_bind_mount(self):
        s = bottle_plan_to_compose(_plan(with_git=True))["services"]["git-gate"]
        # Per-upstream identity file is mounted at /git-gate/creds/<name>-key.
        self.assertTrue(any(
            v["target"] == "/git-gate/creds/upstream-key"
            for v in s["volumes"]
        ))


class TestConditionalEgress(unittest.TestCase):
    def test_absent_when_no_routes(self):
        s = bottle_plan_to_compose(_plan(with_egress=False))["services"]
        self.assertNotIn("egress", s)

    def test_present_when_routes(self):
        s = bottle_plan_to_compose(_plan(with_egress=True))["services"]
        self.assertIn("egress", s)

    def test_egress_alias_on_internal(self):
        s = bottle_plan_to_compose(_plan(with_egress=True))["services"]["egress"]
        self.assertIn("egress", s["networks"]["internal"]["aliases"])

    def test_egress_upstream_envs(self):
        s = bottle_plan_to_compose(_plan(with_egress=True))["services"]["egress"]
        env = s["environment"]
        self.assertIn(
            f"EGRESS_UPSTREAM_PROXY=http://claude-bottle-pipelock-{SLUG}:8888",
            env,
        )
        self.assertIn(
            "EGRESS_UPSTREAM_CA=/home/mitmproxy/.mitmproxy/pipelock-ca.pem",
            env,
        )

    def test_egress_token_slot_bare_name(self):
        # Bare NAME entry in environment list → value inherits from
        # compose process env, never lands in the rendered file.
        s = bottle_plan_to_compose(_plan(with_egress=True))["services"]["egress"]
        self.assertIn("EGRESS_TOKEN_0", s["environment"])

    def test_egress_depends_on_pipelock(self):
        s = bottle_plan_to_compose(_plan(with_egress=True))["services"]["egress"]
        self.assertIn("pipelock", s["depends_on"])

    def test_egress_bind_mounts(self):
        s = bottle_plan_to_compose(_plan(with_egress=True))["services"]["egress"]
        targets = {v["target"] for v in s["volumes"]}
        self.assertEqual(
            {
                "/etc/egress/routes.yaml",
                "/home/mitmproxy/.mitmproxy/mitmproxy-ca.pem",
                "/home/mitmproxy/.mitmproxy/pipelock-ca.pem",
            },
            targets,
        )


class TestConditionalSupervise(unittest.TestCase):
    def test_absent_when_off(self):
        s = bottle_plan_to_compose(_plan(supervise=False))["services"]
        self.assertNotIn("supervise", s)

    def test_present_when_on(self):
        s = bottle_plan_to_compose(_plan(supervise=True))["services"]
        self.assertIn("supervise", s)

    def test_supervise_internal_only(self):
        s = bottle_plan_to_compose(_plan(supervise=True))["services"]["supervise"]
        self.assertEqual({"internal"}, set(s["networks"].keys()))

    def test_supervise_alias_on_internal(self):
        s = bottle_plan_to_compose(_plan(supervise=True))["services"]["supervise"]
        self.assertIn("supervise", s["networks"]["internal"]["aliases"])

    def test_supervise_queue_dir_mounted_rw(self):
        s = bottle_plan_to_compose(_plan(supervise=True))["services"]["supervise"]
        queue_mount = [v for v in s["volumes"] if v["target"] == "/run/supervise/queue"]
        self.assertEqual(1, len(queue_mount))
        self.assertFalse(queue_mount[0]["read_only"])

    def test_supervise_env_vars(self):
        s = bottle_plan_to_compose(_plan(supervise=True))["services"]["supervise"]
        self.assertIn(f"SUPERVISE_BOTTLE_SLUG={SLUG}", s["environment"])


class TestFullMatrix(unittest.TestCase):
    """The eight combinations of git/egress/supervise toggles. Just
    asserts which services appear — content correctness is covered
    per-service above."""

    def test_matrix(self):
        cases: list[tuple[bool, bool, bool, set[str]]] = []
        for g in (False, True):
            for e in (False, True):
                for sv in (False, True):
                    expected = {"pipelock", "agent"}
                    if g:
                        expected.add("git-gate")
                    if e:
                        expected.add("egress")
                    if sv:
                        expected.add("supervise")
                    cases.append((g, e, sv, expected))

        for g, e, sv, expected in cases:
            with self.subTest(git=g, egress=e, supervise=sv):
                s = bottle_plan_to_compose(
                    _plan(with_git=g, with_egress=e, supervise=sv)
                )["services"]
                self.assertEqual(expected, set(s.keys()))


if __name__ == "__main__":
    unittest.main()
