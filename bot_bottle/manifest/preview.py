"""The preflight view of a resolved manifest."""

from __future__ import annotations

from .index import Manifest


def manifest_to_yaml(manifest: Manifest) -> str:
    """The resolved Manifest as YAML for the preflight."""
    lines: list[str] = []

    agent = manifest.agent
    lines.append("agent:")
    if agent.skills:
        lines.append("  skills:")
        for s in agent.skills:
            lines.append(f"    - {s}")
    if not agent.git_user.is_empty():
        lines.append("  git-gate:")
        lines.append("    user:")
        if agent.git_user.name:
            lines.append(f"      name: {agent.git_user.name}")
        if agent.git_user.email:
            lines.append(f"      email: {agent.git_user.email}")

    provider = agent.agent_provider
    if provider.harness != "claude" or provider.dockerfile:
        lines.append("  agent_provider:")
        lines.append(f"    harness: {provider.harness}")
        if provider.dockerfile:
            lines.append(f"    dockerfile: {provider.dockerfile}")

    bottle = manifest.bottle
    lines.append("bottle:")

    if bottle.env:
        lines.append("  env:")
        for k, v in sorted(bottle.env.items()):
            lines.append(f"    {k}: {v}")

    has_git_gate = not bottle.git_user.is_empty() or bottle.git
    if has_git_gate:
        lines.append("  git-gate:")
        if not bottle.git_user.is_empty():
            lines.append("    user:")
            if bottle.git_user.name:
                lines.append(f"      name: {bottle.git_user.name}")
            if bottle.git_user.email:
                lines.append(f"      email: {bottle.git_user.email}")
        if bottle.git:
            lines.append("    repos:")
            for entry in bottle.git:
                lines.append(f"      {entry.Name}:")
                lines.append(f"        url: {entry.Upstream}")

    if bottle.egress.routes:
        lines.append("  egress:")
        lines.append("    routes:")
        for r in bottle.egress.routes:
            lines.append(f"      - host: {r.Host}")
            if r.AuthScheme or r.AuthHeader:
                lines.append("        auth:")
                if r.AuthScheme:
                    lines.append(f"          scheme: {r.AuthScheme}")
                if r.AuthHeader:
                    lines.append(f"          header: {r.AuthHeader}")

    lines.append(f"  supervise: {'true' if bottle.supervise else 'false'}")
    if bottle.nested_containers:
        lines.append("  nested_containers: true")
    if bottle.preserve_container:
        lines.append("  preserve_container: true")
    if resources := bottle.resources.document():
        lines.append("  resources:")
        lines.extend(f"    {k}: {v}" for k, v in resources.items())

    return "\n".join(lines)


__all__ = ["manifest_to_yaml"]
