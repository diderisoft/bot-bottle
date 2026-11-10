"""The `ManifestBottle` value type, separate from the package facade so the
`extends:` and loader resolvers can import it without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .. import secret_ref
from ..lib.config_store import AUDIT_DELIVERIES
from .util import ManifestError, as_json_object
from .egress import ManifestEgressConfig
from .forges import ManifestForge
from .resources import ManifestResources
from .git import (
    ManifestGitEntry, ManifestGitUser, parse_git_gate_actions, parse_git_gate_config,
    parse_git_gate_protect_default_branch, parse_git_gate_trailers,
)
from .schema import BOTTLE_KEYS

__all__ = ["ManifestBottle"]


def _empty_str_dict() -> dict[str, str]:
    return {}


FIREHOSE_MODES = ("full", "metadata")


_WARNED: set[str] = set()


def _warn_unmarked(var: str, value: str) -> None:
    """Warn (once per variable) when a plain `?` or `${VAR}` has a name that
    says it holds a secret: only `secret:` marks one."""
    kind = secret_ref.kind(value)
    if secret_ref.is_secret(value) or kind not in (secret_ref.PROMPT, secret_ref.HOST_ENV) \
            or not secret_ref.secret_looking(var) or var in _WARNED:
        return
    _WARNED.add(var)
    # pylint: disable-next=import-outside-toplevel
    from ..lib.log import warn
    warn(f"{var} isn't marked secret; write secret:? (or secret:${{VAR}}) to treat it as one.")


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
    # `git_gate.trailers`: require, warn or off; "" is unset (require).
    git_trailers: str = ""
    # `git_gate.protect_default_branch`: None is unset (off), so a later
    # bottle can turn off what an earlier one turned on.
    git_protect_default_branch: bool | None = None
    # `git_gate.actions.create`: where the agent may create repos without a
    # card (PRD 0120); unioned across the chain.
    git_create: tuple[str, ...] = ()
    egress: ManifestEgressConfig = field(default_factory=ManifestEgressConfig)
    # Run the supervise server for this bottle (default true).
    supervise: bool = True
    # Guest-local container engine, implemented inside the bottle or rejected.
    # Off by default for its image weight, resident service and relaxed device
    # modes.
    nested_containers: bool = False
    # Keep the bottled agent's guest (container or VM disk) across stop and
    # resume, not only its home: for an agent image that gives the agent root,
    # so what it installs stays (PRD 0110). Off by default.
    preserve_container: bool = False
    audit: ManifestAudit = field(default_factory=ManifestAudit)
    # `resources:` memory and vCPUs; unset keys take the backend's usual size.
    resources: ManifestResources = field(default_factory=ManifestResources)
    # Forges declared in the bottle's chain (`git_gate.forges`), set while
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
                f"Declare upstreams under 'git_gate.repos', as grants "
                f"(<forge>/<owner>/<repo>[:write]) or url + key + host_key; the "
                f"git-gate daemon holds the credential and gitleaks-scans pushes."
            )

        if "git" in d:
            raise ManifestError(
                f"bottle '{name}' uses 'git' which has been replaced by "
                f"'git_gate'. Move git.user → git_gate.user "
                f"and git.remotes → git_gate.repos (grants, or url + key + host_key)."
            )

        if "git_user" in d:
            raise ManifestError(
                f"bottle '{name}' has a 'git_user' field, which has been "
                f"removed. Move it under 'git_gate.user'."
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
                        f"env entry {var} in bottle '{name}' must be a string "
                        f"(was {type(value).__name__}). Write \"?<message>\" (asked at "
                        f"launch), \"${{VAR}}\" (from your environment) or \"secret:NAME\" "
                        f"(from the secrets file); \"secret:?\" and \"secret:${{VAR}}\" "
                        f"mark the first two secret."
                    )
                problem = secret_ref.problem(value)
                if problem:
                    raise ManifestError(f"env entry {var} in bottle '{name}': {problem}")
                _warn_unmarked(var, value)
                env[var] = value

        git: tuple[ManifestGitEntry, ...] = ()
        git_user = ManifestGitUser()
        git_trailers = ""
        protect_default: bool | None = None
        git_create: tuple[str, ...] = ()
        git_raw = d.get("git_gate")
        if git_raw is not None:
            git, git_user = parse_git_gate_config(name, git_raw)
            git_trailers = parse_git_gate_trailers(name, git_raw)
            protect_default = parse_git_gate_protect_default_branch(name, git_raw)
            git_create = parse_git_gate_actions(name, git_raw)

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

        preserve_raw = d.get("preserve_container", False)
        if not isinstance(preserve_raw, bool):
            raise ManifestError(
                f"bottle '{name}' preserve_container must be a boolean "
                f"(was {type(preserve_raw).__name__})"
            )

        audit = ManifestAudit.from_dict(name, d["audit"]) if "audit" in d else ManifestAudit()
        resources = (ManifestResources.from_dict(name, d["resources"]) if "resources" in d
                     else ManifestResources())

        return cls(
            env=env, git=git,
            git_user=git_user, git_trailers=git_trailers,
            git_protect_default_branch=protect_default, git_create=git_create, egress=egress,
            supervise=supervise_raw,
            nested_containers=nested_raw, preserve_container=preserve_raw, audit=audit,
            resources=resources, declared_fields=frozenset(d),
        )
