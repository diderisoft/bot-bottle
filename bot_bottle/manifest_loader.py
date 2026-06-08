"""Internal per-file Markdown manifest loader."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .log import warn
from .manifest_schema import (
    entity_name_from_path,
    validate_agent_frontmatter_keys,
    validate_bottle_frontmatter_keys,
)
from .yaml_subset import YamlSubsetError, parse_frontmatter

if TYPE_CHECKING:
    from .manifest import ManifestAgent, ManifestBottle


def check_stale_json(dir_path: Path, md_dir: Path, label: str) -> None:
    """Die if `<dir_path>/bot-bottle.json` exists but `md_dir` does
    not. The manifest format changed in PRD 0011 and we do not want
    to silently leave the JSON content unused."""
    from .manifest import ManifestError

    legacy = dir_path / "bot-bottle.json"
    if legacy.is_file() and not md_dir.exists():
        raise ManifestError(
            f"found {legacy} but {md_dir} does not exist. The manifest "
            f"format changed in PRD 0011 — rewrite the JSON content "
            f"as per-file Markdown under {md_dir}/bottles/ and "
            f"{md_dir}/agents/. See README.md for the schema. "
            f"({label})"
        )


def load_bottles_from_dir(bottles_dir: Path) -> dict[str, ManifestBottle]:
    """Walk `<bottles_dir>/*.md`, parse each as a bottle, and return
    `{name: Bottle}`. Missing dir returns an empty dict."""
    from .manifest import ManifestError
    from .manifest_extends import resolve_bottles

    raws: dict[str, dict[str, object]] = {}
    if not bottles_dir.is_dir():
        return {}
    for path in sorted(bottles_dir.glob("*.md")):
        name = entity_name_from_path(path)
        if name is None:
            warn(
                f"skipping {path}: filename must match "
                f"[a-z][a-z0-9-]*.md (got {path.name!r})"
            )
            continue
        try:
            fm, _body = parse_frontmatter(path.read_text())
        except OSError as e:
            raise ManifestError(f"could not read {path}: {e}") from e
        except YamlSubsetError as e:
            raise ManifestError(f"{path}: {e}") from e
        validate_bottle_frontmatter_keys(path, fm.keys())
        raws[name] = fm
    return resolve_bottles(raws)


def load_agents_from_dir(
    agents_dir: Path,
    bottle_names: set[str],
    *,
    source: str,  # noqa: F841 — unused, but required by interface
) -> dict[str, ManifestAgent]:
    """Walk `<agents_dir>/*.md`, parse each as an agent, and return
    `{name: Agent}`. The Markdown body becomes the agent's prompt.
    Missing dir returns an empty dict."""
    from .manifest import ManifestAgent, ManifestError

    out: dict[str, ManifestAgent] = {}
    if not agents_dir.is_dir():
        return out
    for path in sorted(agents_dir.glob("*.md")):
        name = entity_name_from_path(path)
        if name is None:
            warn(
                f"skipping {path}: filename must match "
                f"[a-z][a-z0-9-]*.md (got {path.name!r})"
            )
            continue
        try:
            fm, body = parse_frontmatter(path.read_text())
        except OSError as e:
            raise ManifestError(f"could not read {path}: {e}") from e
        except YamlSubsetError as e:
            raise ManifestError(f"{path}: {e}") from e
        validate_agent_frontmatter_keys(path, fm.keys())
        # Build the dict Agent.from_dict expects. The body becomes
        # prompt; Claude Code passthrough fields stay in fm and get
        # ignored by Agent.from_dict (reads bottle/skills/git-gate/prompt).
        agent_dict: dict[str, object] = {
            "bottle": fm.get("bottle"),
            "skills": fm.get("skills", []),
            "prompt": body.strip(),
        }
        if "git-gate" in fm:
            agent_dict["git-gate"] = fm["git-gate"]
        out[name] = ManifestAgent.from_dict(name, agent_dict, bottle_names)
    return out
