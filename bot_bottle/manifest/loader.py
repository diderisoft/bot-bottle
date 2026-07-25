"""Internal per-file Markdown manifest loader."""

from __future__ import annotations

from pathlib import Path

from ..log import warn
from .bottle import ManifestBottle
from .extends import resolve_bottles
from .schema import (
    entity_name_from_path,
    validate_bottle_frontmatter_keys,
)
from .util import ManifestError
from ..yaml_subset import YamlSubsetError, parse_frontmatter


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


def load_bottle_chain_from_dir(
    bottle_name: str, bottles_dir: Path
) -> ManifestBottle:
    """Load `bottle_name` and its full `extends:` chain from `bottles_dir`,
    returning the resolved ManifestBottle.

    Only the files in the extends chain are read — unrelated bottle files
    are never touched. Raises ManifestError on parse or validation failure."""
    raws: dict[str, dict[str, object]] = {}
    to_load = [bottle_name]
    while to_load:
        name = to_load.pop()
        if name in raws:
            continue
        path = bottles_dir / f"{name}.md"
        if not path.is_file():
            avail = ", ".join(
                p.stem for p in sorted(bottles_dir.glob("*.md")) if p.is_file()
            ) or "(none)"
            raise ManifestError(
                f"bottle '{name}' not found at {path}. "
                f"Available: {avail}"
            )
        try:
            fm, _body = parse_frontmatter(path.read_text())
        except OSError as e:
            raise ManifestError(f"could not read {path}: {e}") from e
        except YamlSubsetError as e:
            raise ManifestError(f"{path}: {e}") from e
        validate_bottle_frontmatter_keys(path, fm.keys())
        raws[name] = dict(fm)
        parent = fm.get("extends")
        if isinstance(parent, str):
            to_load.append(parent)
        elif isinstance(parent, list):
            to_load.extend(p for p in parent if isinstance(p, str))

    return resolve_bottles(raws)[bottle_name]
