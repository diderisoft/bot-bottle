"""The `ManifestBottle` value type.

Split out of `manifest.py` so the `extends:`/loader resolvers can import it
without a circular dependency: `manifest.py` imports those resolvers, while
they only need this value type. Everything here depends on leaf modules
(`manifest_util`, `manifest_agent`, `manifest_egress`, `manifest_git`,
`manifest_schema`), so this module sits at the bottom of the manifest layer.

`manifest.py` re-exports `ManifestBottle`, so existing
`from .manifest import ManifestBottle` callers are unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .manifest_util import ManifestError, as_json_object
from .manifest_agent import ManifestAgentProvider
from .manifest_egress import ManifestEgressConfig
from .manifest_git import ManifestGitEntry, ManifestGitUser, parse_git_gate_config
from .manifest_schema import BOTTLE_KEYS

__all__ = ["ManifestBottle"]


def _empty_str_dict() -> dict[str, str]:
    return {}


@dataclass(frozen=True)
class ManifestBottle:
    env: Mapping[str, str] = field(default_factory=_empty_str_dict)
    agent_provider: ManifestAgentProvider = field(default_factory=ManifestAgentProvider)
    git: tuple[ManifestGitEntry, ...] = ()
    # Per-bottle git identity (issue #86). Empty default — bottles
    # that don't set `git-gate.user:` in the manifest skip the
    # `git config --global` step entirely. A bottle can declare a user
    # identity without any git-gate.repos upstreams, and vice versa.
    git_user: ManifestGitUser = field(default_factory=ManifestGitUser)
    egress: ManifestEgressConfig = field(default_factory=ManifestEgressConfig)
    # Per-bottle stuck-recovery daemon (PRD 0013). When true (the
    # default, issue #249), the launch step brings up a supervise
    # daemon that exposes egress MCP tools to the agent. Set
    # `supervise: false` to skip the gateway.
    supervise: bool = True
    # Guest-local container engine (issue #392). Not a host-daemon grant:
    # backends implement it inside the bottle or reject it. Gated because it
    # costs image weight, a resident service, and relaxed guest device modes
    # that the majority of bottles never need.
    nested_containers: bool = False
    # Source fields retained across extends/runtime composition. Boolean
    # defaults otherwise erase the distinction between "omitted" and an
    # explicitly declared value (especially False).
    declared_fields: frozenset[str] = frozenset()

    @classmethod
    def from_dict(cls, name: str, raw: object) -> "ManifestBottle":
        d = as_json_object(raw, f"bottle '{name}'")

        if "runtime" in d:
            raise ManifestError(
                f"bottle '{name}' has a 'runtime' field, which is no longer "
                f"supported. gVisor (runsc) is now auto-detected by the "
                f"backend; remove the 'runtime' field from the bottle "
                f"definition."
            )

        if "ssh" in d:
            raise ManifestError(
                f"bottle '{name}' has an 'ssh' field, which has been removed "
                f"(PRD 0009). Declare upstreams under 'git-gate.repos' with "
                f"url + identity + host_key; the git-gate daemon (PRD 0008) "
                f"holds the credential and gitleaks-scans pushes."
            )

        if "git" in d:
            raise ManifestError(
                f"bottle '{name}' uses 'git' which has been replaced by "
                f"'git-gate' (PRD 0047). Move git.user → git-gate.user "
                f"and git.remotes → git-gate.repos (fields: url, identity, host_key)."
            )

        if "git_user" in d:
            raise ManifestError(
                f"bottle '{name}' has a 'git_user' field, which has been "
                f"removed. Move it under 'git-gate.user'."
            )

        unknown = set(d.keys()) - BOTTLE_KEYS
        if unknown:
            allowed = ", ".join(sorted(BOTTLE_KEYS))
            raise ManifestError(
                f"bottle '{name}' has unknown key(s) {sorted(unknown)}; "
                f"allowed keys are {allowed}."
            )

        env: dict[str, str] = {}
        env_raw = d.get("env")
        if env_raw is not None:
            env_dict = as_json_object(env_raw, f"bottle '{name}' env")
            for var, value in env_dict.items():
                if not isinstance(value, str):
                    raise ManifestError(
                        f"env entry {var} in bottle '{name}' must be a JSON string "
                        f"(was {type(value).__name__}). Use \"?<message>\" for prompt-at-runtime."
                    )
                env[var] = value

        git: tuple[ManifestGitEntry, ...] = ()
        git_user = ManifestGitUser()
        git_raw = d.get("git-gate")
        if git_raw is not None:
            git, git_user = parse_git_gate_config(name, git_raw)

        agent_provider = (
            ManifestAgentProvider.from_dict(name, d["agent_provider"])
            if "agent_provider" in d
            else ManifestAgentProvider()
        )

        egress = (
            ManifestEgressConfig.from_dict(name, d["egress"])
            if "egress" in d
            else ManifestEgressConfig()
        )

        supervise_raw = d.get("supervise", True)
        if not isinstance(supervise_raw, bool):
            raise ManifestError(
                f"bottle '{name}' supervise must be a boolean "
                f"(was {type(supervise_raw).__name__})"
            )

        nested_raw = d.get("nested_containers", False)
        if not isinstance(nested_raw, bool):
            raise ManifestError(
                f"bottle '{name}' nested_containers must be a boolean "
                f"(was {type(nested_raw).__name__})"
            )

        return cls(
            env=env, agent_provider=agent_provider, git=git,
            git_user=git_user, egress=egress, supervise=supervise_raw,
            nested_containers=nested_raw,
            declared_fields=frozenset(d),
        )
