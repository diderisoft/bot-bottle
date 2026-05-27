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
from claude_bottle.backend.docker.compose import (
    COMPOSE_PROJECT_PREFIX,
    bottle_plan_to_compose,
    compose_project_name,
    slug_from_compose_project,
)
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

    def test_internal_network_is_internal(self):
        spec = bottle_plan_to_compose(_plan())
        net = spec["networks"]["internal"]
        self.assertEqual(f"claude-bottle-net-{SLUG}", net["name"])
        self.assertTrue(net["internal"])

    def test_egress_network_is_external_bridge(self):
        spec = bottle_plan_to_compose(_plan())
        net = spec["networks"]["egress"]
        self.assertEqual(f"claude-bottle-egress-{SLUG}", net["name"])
        # No `internal:` key on the egress network — defaults to a
        # normal user-defined bridge.
        self.assertNotIn("internal", net)


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


class TestSidecarBundleShape(unittest.TestCase):
    """PRD 0024 chunk 2: with CLAUDE_BOTTLE_SIDECAR_BUNDLE=1 set,
    the renderer emits a `sidecars` service in place of the four
    per-sidecar services. The legacy four-sidecar tests above
    cover the flag-off shape; these lock down the flag-on shape."""

    def _render(self, **plan_kwargs):
        from unittest.mock import patch
        with patch.dict("os.environ", {"CLAUDE_BOTTLE_SIDECAR_BUNDLE": "1"}):
            return bottle_plan_to_compose(_plan(**plan_kwargs))

    def test_emits_two_services_minimal(self):
        spec = self._render()
        self.assertEqual({"sidecars", "agent"}, set(spec["services"].keys()))

    def test_emits_two_services_full_matrix(self):
        spec = self._render(with_git=True, with_egress=True, supervise=True)
        # Still two services — the bundle absorbs git-gate/egress/supervise.
        self.assertEqual({"sidecars", "agent"}, set(spec["services"].keys()))

    def test_bundle_uses_bundle_image_and_dockerfile(self):
        sc = self._render()["services"]["sidecars"]
        self.assertEqual("claude-bottle-sidecars:latest", sc["image"])
        self.assertEqual("Dockerfile.sidecars", sc["build"]["dockerfile"])

    def test_bundle_container_name_uses_sidecars_prefix(self):
        sc = self._render()["services"]["sidecars"]
        self.assertEqual(f"claude-bottle-sidecars-{SLUG}", sc["container_name"])

    def test_bundle_joins_both_networks(self):
        sc = self._render()["services"]["sidecars"]
        self.assertEqual({"internal", "egress"}, set(sc["networks"].keys()))

    def test_internal_aliases_cover_pipelock_and_egress_shortnames(self):
        # The agent's HTTPS_PROXY url references either `egress` or
        # `pipelock` (long form). Both must resolve to the bundle.
        sc = self._render()["services"]["sidecars"]
        aliases = set(sc["networks"]["internal"]["aliases"])
        self.assertIn("egress", aliases)
        self.assertIn(f"claude-bottle-pipelock-{SLUG}", aliases)
        self.assertIn(f"claude-bottle-egress-{SLUG}", aliases)

    def test_internal_aliases_omit_inactive_sidecars(self):
        # With no git-gate / supervise, those names are NOT aliased
        # — keeps the alias list honest about what's actually
        # listening inside the bundle.
        sc = self._render()["services"]["sidecars"]
        aliases = set(sc["networks"]["internal"]["aliases"])
        self.assertNotIn(f"claude-bottle-git-gate-{SLUG}", aliases)
        self.assertNotIn("supervise", aliases)

    def test_internal_aliases_include_active_sidecars(self):
        sc = self._render(with_git=True, supervise=True)["services"]["sidecars"]
        aliases = set(sc["networks"]["internal"]["aliases"])
        self.assertIn(f"claude-bottle-git-gate-{SLUG}", aliases)
        self.assertIn("supervise", aliases)
        self.assertIn(f"claude-bottle-supervise-{SLUG}", aliases)

    def test_daemons_csv_lists_only_active(self):
        # Egress + pipelock are always in the daemon set even when
        # the bottle has no routes (egress falls back to regular@9099
        # and is just unused; cheaper than special-casing).
        sc = self._render()["services"]["sidecars"]
        daemons = {
            line.split("=", 1)[1]
            for line in sc["environment"]
            if line.startswith("CLAUDE_BOTTLE_SIDECAR_DAEMONS=")
        }
        self.assertEqual({"egress,pipelock"}, daemons)

    def test_daemons_csv_expands_with_optional_sidecars(self):
        sc = self._render(with_git=True, supervise=True)["services"]["sidecars"]
        for line in sc["environment"]:
            if line.startswith("CLAUDE_BOTTLE_SIDECAR_DAEMONS="):
                csv = line.split("=", 1)[1]
                break
        else:
            self.fail("CLAUDE_BOTTLE_SIDECAR_DAEMONS not in env")
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
        # supervise queue dir target = QUEUE_DIR_IN_CONTAINER
        self.assertTrue(any("supervise/queue" in t or t.startswith("/run/supervise")
                            for t in targets))

    def test_extra_hosts_emitted_for_git_upstreams(self):
        sc = self._render(with_git=True)["services"]["sidecars"]
        self.assertIn("example.com:10.0.0.1", sc.get("extra_hosts", []))

    def test_extra_hosts_omitted_when_no_git(self):
        sc = self._render()["services"]["sidecars"]
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


class TestSidecarBundleFlag(unittest.TestCase):
    """The flag's parser: covers the cases sidecar_bundle_enabled
    has to interpret correctly so an operator-supplied value lands
    in the expected shape."""

    def _enabled(self, value: str | None) -> bool:
        from claude_bottle.backend.docker.sidecar_bundle import (
            sidecar_bundle_enabled,
        )
        env: dict[str, str] = {}
        if value is not None:
            env["CLAUDE_BOTTLE_SIDECAR_BUNDLE"] = value
        return sidecar_bundle_enabled(env)

    def test_unset_disabled(self):
        self.assertFalse(self._enabled(None))

    def test_empty_disabled(self):
        self.assertFalse(self._enabled(""))

    def test_zero_disabled(self):
        self.assertFalse(self._enabled("0"))

    def test_false_disabled(self):
        self.assertFalse(self._enabled("false"))
        self.assertFalse(self._enabled("FALSE"))

    def test_no_disabled(self):
        self.assertFalse(self._enabled("no"))
        self.assertFalse(self._enabled("off"))

    def test_one_enabled(self):
        self.assertTrue(self._enabled("1"))

    def test_true_enabled(self):
        self.assertTrue(self._enabled("true"))

    def test_arbitrary_truthy_enabled(self):
        # The flag treats anything other than the known falsy
        # values as truthy — operator typos default to "enabled"
        # which is the safer interpretation for an opt-in flag.
        self.assertTrue(self._enabled("yes"))


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


if __name__ == "__main__":
    unittest.main()
