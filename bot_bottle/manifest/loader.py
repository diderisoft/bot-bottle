"""Internal per-file Markdown manifest loader."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..lib.log import warn
from .bottle import ManifestBottle
from .extends import resolve_bottles
from .schema import (
    entity_name_from_path,
    validate_bottle_frontmatter_keys,
)
from .util import ManifestError
from ..contract.yaml_subset import YamlSubsetError, parse_frontmatter


def check_stale_json(dir_path: Path, md_dir: Path, label: str) -> None:
    """Die if `<dir_path>/bot-bottle.json` exists but `md_dir` does
    not. The manifest format changed to per-file Markdown and we do not
    want to silently leave the JSON content unused."""
    legacy = dir_path / "bot-bottle.json"
    if legacy.is_file() and not md_dir.exists():
        raise ManifestError(
            f"found {legacy} but {md_dir} does not exist. The manifest "
            f"format changed — rewrite the JSON content "
            f"as per-file Markdown under {md_dir}/bottles/ and "
            f"{md_dir}/agents/. See README.md for the schema. "
            f"({label})"
        )


def scan_bottle_names(bottles_dir: Path) -> list[str]:
    """Scan `<bottles_dir>/*.md` for valid filenames and return sorted bottle names.

    No file content is read. Invalid filenames are skipped with a warning."""
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
    """Scan `<agents_dir>/*.md` for valid filenames and return `{name: path}`.

    No file content is read. Invalid filenames are skipped with a warning."""
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


# The built-in bottles shipped with bot-bottle (`basic`).
BUILTIN_BOTTLES_DIR = Path(__file__).parent / "builtin"


@dataclass(frozen=True)
class BottleSource:
    """One file in a resolved bottle chain, and whether it came from the
    project (`./.bot-bottle/bottles/`), which makes it untrusted input."""

    name: str
    path: Path
    project: bool


def load_bottle_chain(
    bottle_name: str, dirs: Sequence[tuple[Path, bool]],
) -> tuple[ManifestBottle, tuple[BottleSource, ...]]:
    """Load `bottle_name` and its `extends:` chain, each name from the first
    of ``dirs`` that has it (``(dir, is_project)`` pairs, in precedence
    order). Returns the resolved bottle and the files it came from."""
    raws: dict[str, dict[str, object]] = {}
    sources: list[BottleSource] = []
    to_load = [bottle_name]
    while to_load:
        name = to_load.pop()
        if name in raws:
            continue
        found = next(((d / f"{name}.md", project) for d, project in dirs
                      if (d / f"{name}.md").is_file()), None)
        if found is None:
            avail = ", ".join(sorted({p.stem for d, _ in dirs if d.is_dir()
                                      for p in d.glob("*.md") if p.is_file()})) or "(none)"
            raise ManifestError(f"bottle '{name}' not found. Available: {avail}")
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

    return resolve_bottles(raws)[bottle_name], tuple(sources)


def load_bottle_chain_from_dir(
    bottle_name: str, bottles_dir: Path
) -> ManifestBottle:
    """Load `bottle_name` and its full `extends:` chain from `bottles_dir`,
    returning the resolved ManifestBottle.

    Only the files in the extends chain are read — unrelated bottle files
    are never touched. Raises ManifestError on parse or validation failure."""
    return load_bottle_chain(bottle_name, ((bottles_dir, False),))[0]
