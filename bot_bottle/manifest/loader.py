"""Per-file Markdown manifest loader."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..lib.log import warn
from .bottle import ManifestBottle
from .extends import resolve_bottles
from .legacy_git_gate import OldGitGateFormatError, forges_by_host, scan_bottles
from .schema import (
    entity_name_from_path,
    validate_bottle_frontmatter_keys,
)
from .util import ManifestError
from ..contract.yaml_subset import YamlSubsetError, parse_frontmatter


def check_stale_json(dir_path: Path, md_dir: Path, label: str) -> None:
    """Die if `<dir_path>/bot-bottle.json` exists without `md_dir`, rather than
    silently ignore the old format."""
    legacy = dir_path / "bot-bottle.json"
    if legacy.is_file() and not md_dir.exists():
        raise ManifestError(
            f"found {legacy} but {md_dir} does not exist. The manifest "
            f"format changed — rewrite the JSON content "
            f"as per-file Markdown under {md_dir}/bottles/ and "
            f"{md_dir}/agents/. See docs/reference/manifest.md for the schema. "
            f"({label})"
        )


def scan_bottle_names(bottles_dir: Path) -> list[str]:
    """Sorted bottle names from `<bottles_dir>/*.md` filenames; invalid names
    are skipped with a warning. Reads no content."""
    result: list[str] = []
    if not bottles_dir.is_dir():
        return result
    for path in sorted(bottles_dir.glob("*.md")):
        name = entity_name_from_path(path)
        if name is None:
            warn(
                f"skipping {path}: filename must match "
                f"[a-z][a-z0-9-]*.md (got {path.name!r})"
            )
            continue
        result.append(name)
    return result


def scan_agent_names(agents_dir: Path) -> dict[str, Path]:
    """`{name: path}` from `<agents_dir>/*.md` filenames; invalid names are
    skipped with a warning. Reads no content."""
    result: dict[str, Path] = {}
    if not agents_dir.is_dir():
        return result
    for path in sorted(agents_dir.glob("*.md")):
        name = entity_name_from_path(path)
        if name is None:
            warn(
                f"skipping {path}: filename must match "
                f"[a-z][a-z0-9-]*.md (got {path.name!r})"
            )
            continue
        result[name] = path
    return result


# Built-in bottles (`basic`).
BUILTIN_BOTTLES_DIR = Path(__file__).parent / "builtin"


@dataclass(frozen=True)
class BottleSource:
    """One file of a resolved bottle chain, and whether it's from the project
    (untrusted input)."""

    name: str
    path: Path
    project: bool


# A project's bottles are named `./<name>`; a bare name is a host or built-in
# bottle, so the two never shadow each other (PRD 0103).
PROJECT_PREFIX = "./"
# A project's default bottle: what `bot-bottle` uses there, and what
# onboarding writes.
PROJECT_BOTTLE = "main"


def bottle_ref(name: str, project: bool) -> str:
    """How a bottle file is referred to: `./<name>` for a project's."""
    return PROJECT_PREFIX + name if project else name


def find_bottle(name: str, dirs: Sequence[tuple[Path, bool]]) -> tuple[Path, bool] | None:
    """`name`'s file and whether it's a project bottle: `./<name>` from the
    project's directory, a bare name from the others, the first that has it."""
    project = name.startswith(PROJECT_PREFIX)
    stem = name[len(PROJECT_PREFIX):] if project else name
    return next(((d / f"{stem}.md", is_project) for d, is_project in dirs
                 if is_project == project and (d / f"{stem}.md").is_file()), None)


def load_bottle_chain(
    bottle_name: str, dirs: Sequence[tuple[Path, bool]],
) -> tuple[ManifestBottle, tuple[BottleSource, ...]]:
    """Load `bottle_name` and its `extends:` chain, each name from the first of
    `dirs` (`(dir, is_project)`, in precedence order) that has it. Returns the
    resolved bottle and its files."""
    raws: dict[str, dict[str, object]] = {}
    sources: list[BottleSource] = []
    to_load = [bottle_name]
    while to_load:
        name = to_load.pop()
        if name in raws:
            continue
        found = find_bottle(name, dirs)
        if found is None:
            avail = ", ".join(sorted({bottle_ref(p.stem, project) for d, project in dirs
                                      if d.is_dir() for p in d.glob("*.md")
                                      if p.is_file()})) or "(none)"
            hint = ("; a project's bottles are named ./<name>"
                    if not name.startswith(PROJECT_PREFIX)
                    and any(project and (d / f"{name}.md").is_file() for d, project in dirs)
                    else "")
            raise ManifestError(f"bottle '{name}' not found. Available: {avail}{hint}")
        path, project = found
        try:
            fm, _body = parse_frontmatter(path.read_text())
        except OSError as e:
            raise ManifestError(f"could not read {path}: {e}") from e
        except YamlSubsetError as e:
            raise ManifestError(f"{path}: {e}") from e
        validate_bottle_frontmatter_keys(path, fm.keys())
        raws[name] = dict(fm)
        sources.append(BottleSource(name, path, project))
        parent = fm.get("extends")
        if isinstance(parent, str):
            to_load.append(parent)
        elif isinstance(parent, list):
            to_load.extend(p for p in parent if isinstance(p, str))

    try:
        return resolve_bottles(raws)[bottle_name], tuple(sources)
    except OldGitGateFormatError as e:
        # Say whether a forge bottle for it exists anywhere, not only in the chain.
        raise e.explained(forge_bottles=forges_by_host(scan_bottles(dirs))) from None
