"""Shared in-memory `DockerBottlePlan` fixture for docker-backend tests.

A fully-resolved plan with toggles for the conditional-service matrix
(git-gate / egress / supervise / canary). Consumed by the consolidated
compose tests; kept here (rather than in a test module) so it survives
independent of any one test file.
"""

from __future__ import annotations

from pathlib import Path

from bot_bottle.contract.hostctl import Settings
from bot_bottle.secret_source import MappingSecrets
from bot_bottle.agent_provider import AgentProvisionPlan
from bot_bottle.backend import BottleSpec
from bot_bottle.backend.docker.bottle_plan import DockerBottlePlan
from bot_bottle.egress import EgressPlan, EgressRoute
from bot_bottle.control.git_gate_render import GitGateUpstream
from bot_bottle.git_gate import GitGatePlan
from bot_bottle.manifest import ManifestIndex
from bot_bottle.supervisor.plan import SupervisePlan


SLUG = "demo-abc12"
STAGE = Path("/tmp/cb-stage")
STATE = Path("/tmp/cb-state")

# Exported to consumers (e.g. test_consolidated_compose); named with a
# leading underscore for the historical fixture convention.
__all__ = ["_plan"]


def _manifest(*, supervise: bool, with_git: bool, with_egress: bool) -> ManifestIndex:
    """Minimal manifest with the toggles the matrix needs. The renderer
    only reads from the plan, not the manifest, so this is just here to
    back BottleSpec."""
    bottle: dict[str, object] = {}
    if supervise:
        bottle["supervise"] = True
    if with_git:
        bottle["git-gate"] = {"repos": {
            "upstream": {
                "url": "ssh://git@example.com:22/x/y.git",
                "key": {"provider": "static", "path": "/etc/hostname"},
            },
        }}
    if with_egress:
        bottle["egress"] = {
            "routes": [{
                "host": "api.example",
                "inspect": {
                    "auth": {"type": "bearer", "value": "secret:TOK"},
                },
            }],
        }
    return ManifestIndex.from_json_obj({
        "bottles": {"dev": bottle},
        "agents": {"demo": {"skills": [], "prompt": ""}},
    })


def _git_gate_plan(upstreams: tuple[GitGateUpstream, ...] = ()) -> GitGatePlan:
    return GitGatePlan(
        slug=SLUG,
        hook_script=STATE / "git-gate" / "pre-receive",
        access_hook_script=STATE / "git-gate" / "access-hook",
        upstreams=upstreams,
    )


def _egress_plan(
    routes: tuple[EgressRoute, ...] = (),
    *,
    canary: bool = False,
) -> EgressPlan:
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
        mitmproxy_ca_cert_only_host_path=STATE / "egress-ca" / "ca.pem",
        canary="fake-canary-value" if canary else "",
        canary_env="CANON_ALPHA_SECRET" if canary else "",
    )


def _supervise_plan() -> SupervisePlan:
    return SupervisePlan(
        slug=SLUG,
    )


def _plan(
    *,
    with_git: bool = False,
    with_egress: bool = False,
    supervise: bool = False,
    canary: bool = False,
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
            roles=(),
        ),)

    index = _manifest(supervise=supervise, with_git=with_git, with_egress=with_egress)
    spec = BottleSpec(
        manifest=index,
        agent_name="demo", bottle_names=("dev",),
        copy_cwd=False,
        user_cwd="/tmp/x",
    )
    return DockerBottlePlan(
        spec=spec,
        manifest=index.load_for_agent("demo", ("dev",)),
        stage_dir=STAGE,
        slug=SLUG,
        forwarded_env={"CLAUDE_CODE_OAUTH_TOKEN": "x"},
        git_gate_plan=_git_gate_plan(upstreams),
        egress_plan=_egress_plan(routes, canary=canary),
        supervise_plan=_supervise_plan() if supervise else None,
        use_runsc=False,
        agent_provision=AgentProvisionPlan(
            harness="claude",
            command="claude",
            prompt_mode="append_file",
            image="bot-bottle-claude:latest",
            dockerfile="",
            guest_home="/home/node",
            instance_name=f"bot-bottle-{SLUG}",
            prompt_file=STAGE / "prompt",
            guest_env={},
        ),
        settings=Settings(),
        secrets=MappingSecrets({}),
    )
