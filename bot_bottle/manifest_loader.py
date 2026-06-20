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
from .manifest_util import ManifestError
from .yaml_subset import YamlSubsetError, parse_frontmatter

if TYPE_CHECKING:
    from .manifest import ManifestAgent, ManifestBottle


def check_stale_json(dir_path: Path, md_dir: Path, label: str) -> None:
    """Die if `<dir_path>/bot-bottle.json` exists but `md_dir` does
    not. The manifest format changed in PRD 0011 and we do not want
    to silently leave the JSON content unused."""
    legacy = dir_path / "bot-bottle.json"
    if legacy.is_file() and not md_dir.exists():
        raise ManifestError(
            f"found {legacy} but {md_dir} does not exist. The manifest "
            f"format changed in PRD 0011 — rewrite the JSON content "
            f"as per-file Markdown under {md_dir}/bottles/ and "
            f"{md_dir}/agents/. See README.md for the schema. "
            f"({label})"
        )


def load_bottles_from_dir(
    bottles_dir: Path,
) -> tuple[dict[str, ManifestBottle], dict[str, ManifestError]]:
    """Walk `<bottles_dir>/*.md`, parse each as a bottle, and return
    `({name: Bottle}, {name: error})`. Missing dir returns empty dicts.

    Per-file errors are collected in the second dict rather than raised,
    so an invalid bottle file does not block unrelated bottles or agents."""
    from .manifest_extends import resolve_bottles_partial

    raws: dict[str, dict[str, object]] = {}
    broken: dict[str, ManifestError] = {}
    if not bottles_dir.is_dir():
        return {}, {}
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
            broken[name] = ManifestError(f"could not read {path}: {e}")
            continue
        except YamlSubsetError as e:
            broken[name] = ManifestError(f"{path}: {e}")
            continue
        try:
            validate_bottle_frontmatter_keys(path, fm.keys())
        except ManifestError as e:
            broken[name] = e
            continue
        raws[name] = fm

    good, resolve_broken = resolve_bottles_partial(raws)
    broken.update(resolve_broken)
    return good, broken


def load_agents_from_dir(
    agents_dir: Path,
    bottle_names: set[str],
    *,
    source: str,  # noqa: F841 — unused, but required by interface
    broken_bottle_errors: dict[str, ManifestError] | None = None,
) -> tuple[dict[str, ManifestAgent], dict[str, ManifestError]]:
    """Walk `<agents_dir>/*.md`, parse each as an agent, and return
    `({name: Agent}, {name: error})`. The Markdown body becomes the
    agent's prompt. Missing dir returns empty dicts.

    Per-file errors are collected in the second dict rather than raised.
    Agents referencing a broken bottle are also moved to the error dict
    so their error surfaces at preflight rather than manifest load time."""
    from .manifest import ManifestAgent

    broken_bottles = broken_bottle_errors or {}
    # Agents may reference bottles that failed to resolve; accept those names
    # during structural parsing so we can detect the broken-bottle case below.
    all_known_bottles = bottle_names | set(broken_bottles.keys())

    out: dict[str, ManifestAgent] = {}
    broken: dict[str, ManifestError] = {}
    if not agents_dir.is_dir():
        return out, broken
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
            broken[name] = ManifestError(f"could not read {path}: {e}")
            continue
        except YamlSubsetError as e:
            broken[name] = ManifestError(f"{path}: {e}")
            continue
        try:
            validate_agent_frontmatter_keys(path, fm.keys())
            agent_dict: dict[str, object] = {
                "bottle": fm.get("bottle"),
                "skills": fm.get("skills", []),
                "prompt": body.strip(),
            }
            if "git-gate" in fm:
                agent_dict["git-gate"] = fm["git-gate"]
            agent = ManifestAgent.from_dict(name, agent_dict, all_known_bottles)
        except ManifestError as e:
            broken[name] = e
            continue

        # Agent parsed fine but its bottle may have failed to resolve.
        bottle_ref = agent.bottle
        if bottle_ref in broken_bottles:
            broken[name] = ManifestError(
                f"agent '{name}' references bottle '{bottle_ref}' which "
                f"failed to load: {broken_bottles[bottle_ref]}"
            )
            continue

        out[name] = agent
    return out, broken
