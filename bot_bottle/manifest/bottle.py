"""The `ManifestBottle` value type, separate from the package facade so the
`extends:` and loader resolvers can import it without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .util import ManifestError, as_json_object
from .egress import ManifestEgressConfig
from .forges import ManifestForge
from .git import (
    ManifestGitEntry, ManifestGitUser, parse_git_gate_config, parse_git_gate_trailers,
)
from .schema import BOTTLE_KEYS

__all__ = ["ManifestBottle"]


def _empty_str_dict() -> dict[str, str]:
    return {}


AUDIT_DELIVERIES = ("best_effort", "required")
FIREHOSE_MODES = ("full", "metadata")


@dataclass(frozen=True)
class ManifestAudit:
    """`audit:` in a bottle; empty fields weren't set.

    - `delivery`: `best_effort` records the gap and goes ahead, `required`
      refuses an action whose event can't be written. Unset: the host default.
    - `firehose`: `full` (default) records each request as sent, bodies
      included; `metadata` records only method, host, port, decision, status,
      size and timing."""

    delivery: str = ""
    firehose: str = ""

    @classmethod
    def from_dict(cls, name: str, raw: object) -> "ManifestAudit":
        d = as_json_object(raw, f"bottle '{name}' audit")
        unknown = set(d) - {"delivery", "firehose"}
        if unknown:
            raise ManifestError(
                f"bottle '{name}' audit has unknown key(s) {sorted(unknown)}; "
                f"the accepted keys are 'delivery' and 'firehose'"
            )
        for key, allowed in (("delivery", AUDIT_DELIVERIES), ("firehose", FIREHOSE_MODES)):
            if key in d and d[key] not in allowed:
                raise ManifestError(
                    f"bottle '{name}' audit.{key} must be one of {', '.join(allowed)}"
                )
        return cls(delivery=str(d.get("delivery", "")), firehose=str(d.get("firehose", "")))


@dataclass(frozen=True)
class ManifestBottle:
    env: Mapping[str, str] = field(default_factory=_empty_str_dict)
    git: tuple[ManifestGitEntry, ...] = ()
    # The bottle's git identity; empty skips `git config`. Independent of
    # repos.
    git_user: ManifestGitUser = field(default_factory=ManifestGitUser)
    # `git-gate.trailers`: require, warn or off; "" is unset (require).
    git_trailers: str = ""
    egress: ManifestEgressConfig = field(default_factory=ManifestEgressConfig)
    # Run the supervise server for this bottle (default true).
    supervise: bool = True
    # Guest-local container engine, implemented inside the bottle or rejected.
    # Off by default for its image weight, resident service and relaxed device
    # modes.
    nested_containers: bool = False
    audit: ManifestAudit = field(default_factory=ManifestAudit)
    # Forges declared in the bottle's chain (`git-gate.forges`), set while
    # `extends` is resolved.
    forges: tuple[ManifestForge, ...] = ()
    # Fields the source declared, so an explicit `false` survives extends
    # merging.
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
                f"bottle '{name}' has an 'ssh' field, which has been removed. "
                f"Declare upstreams under 'git-gate.repos' with "
                f"url + identity + host_key; the git-gate daemon "
                f"holds the credential and gitleaks-scans pushes."
            )

        if "git" in d:
            raise ManifestError(
                f"bottle '{name}' uses 'git' which has been replaced by "
                f"'git-gate'. Move git.user → git-gate.user "
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
        git_trailers = ""
        git_raw = d.get("git-gate")
        if git_raw is not None:
            git, git_user = parse_git_gate_config(name, git_raw)
            git_trailers = parse_git_gate_trailers(name, git_raw)

        if "agent_provider" in d:
            raise ManifestError(
                f"bottle '{name}' declares agent_provider, which now belongs to "
                "the agent: move it into the agent's manifest "
                "(agents/<name>.md), renaming `template` to `harness`"
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

        audit = ManifestAudit.from_dict(name, d["audit"]) if "audit" in d else ManifestAudit()

        return cls(
            env=env, git=git,
            git_user=git_user, git_trailers=git_trailers, egress=egress,
            supervise=supervise_raw,
            nested_containers=nested_raw, audit=audit,
            declared_fields=frozenset(d),
        )
