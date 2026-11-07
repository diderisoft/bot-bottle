"""bottle-architect: the `bottle-architect` agent, run in its own bottle,
produces a typed proposal (PRD 0117).

The agent is the file `setup` writes, `~/.bot-bottle/agents/bottle-architect.md`:
its body is the system prompt, `agent_provider.harness` picks the harness and
`model:` the model. Only the host's agent is read; a project can't replace it.

It runs in the `bottle-architect` bottle (`suggest/bottles/`): its model API
and nothing else, no repos, no secrets, an empty workspace. That bottle, not
a tool-less mode, is what contains it, so any harness can be the architect.
The host controller keeps it warm (`control/hostctl/architect_bottle.py`);
each suggestion is a fresh headless call, so tasks never share a
conversation. Its answer is parsed here and then validated against the
host's bottles.

Input to the model: the task, the host's bottle library (names, about text,
extends, elements), project metadata (origin URL, the project bottle's name,
and lockfile/manifest *file names* — never their contents), connected forges,
and secret names. The model never sees secret values or file contents.

Any reason the architect can't answer raises `ArchitectError`; the caller
(`suggest.flow`) falls back to the rules path and says why.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..paths import bot_bottle_root
from .proposal import Proposal
from .starter import ARCHITECT_AGENT

TIMEOUT_SECONDS = 120

# A model name goes on the harness's command line; keep it to a plain id.
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,99}")

# Runs the architect on a prompt and returns its answer's text; the host
# controller's warm bottle is the one runner (`ArchitectBottle.run`).
Runner = Callable[["Architect", str], str]


class ArchitectError(Exception):
    """Why bottle-architect didn't answer; the rules path is used instead."""


@dataclass(frozen=True)
class Architect:
    """The `bottle-architect` agent as the suggester runs it."""

    path: Path
    prompt: str
    harness: str
    model: str = ""


def load_architect(host_root: Path) -> Architect:
    """The host's `bottle-architect` agent. ArchitectError when it's missing
    or doesn't load (an unknown harness doesn't load)."""
    # pylint: disable=import-outside-toplevel
    from ..contract.yaml_subset import YamlSubsetError, parse_frontmatter
    from ..manifest.agent import ManifestAgent
    from ..manifest.schema import validate_agent_frontmatter_keys
    from ..manifest.util import ManifestError

    path = host_root / "agents" / f"{ARCHITECT_AGENT}.md"
    try:
        fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        validate_agent_frontmatter_keys(path, fm.keys())
        raw: dict[str, object] = {"prompt": body.strip()}
        if "agent_provider" in fm:
            raw["agent_provider"] = fm["agent_provider"]
        agent = ManifestAgent.from_dict(ARCHITECT_AGENT, raw)
    except FileNotFoundError as e:
        raise ArchitectError(f"there's no {ARCHITECT_AGENT} agent at {path}; "
                             "`bot-bottle setup` writes it") from e
    except (OSError, YamlSubsetError, ManifestError) as e:
        raise ArchitectError(f"{path} doesn't load: {e}") from e
    if not agent.prompt:
        raise ArchitectError(f"{path} has no prompt")
    model = fm.get("model", "")
    if not isinstance(model, str) or (model and not _MODEL_RE.fullmatch(model)):
        raise ArchitectError(f"{path}: model must be a plain model name")
    harness = agent.agent_provider.harness
    try:
        # pylint: disable-next=import-outside-toplevel
        from ..agent_provider import get_provider
        get_provider(harness)  # a built-in harness or a user plugin
    except ValueError as e:
        raise ArchitectError(f"{path} doesn't load: {e}") from e
    return Architect(path=path, prompt=agent.prompt, harness=harness, model=model)


def _host_library(host_root: Path, dirs: list[tuple[Path, bool]]) -> list[dict[str, Any]]:
    """Each bottle the host knows: name, about text (body first line), extends,
    and the secrets it needs that this host lacks."""
    from ..control.hostctl.manifest_elements import bottle_elements
    from .runnable import host_secrets, library_facts
    secrets = host_secrets(path=host_root / "secrets")
    from ..manifest.loader import scan_bottle_names
    from ..contract.yaml_subset import parse_frontmatter, YamlSubsetError

    result = []
    seen: set[str] = set()
    for directory, is_project in dirs:
        if not directory.is_dir():
            continue
        for name in scan_bottle_names(directory):
            full_name = f"./{name}" if is_project else name
            if full_name in seen:
                continue
            seen.add(full_name)
            path = directory / f"{name}.md"
            try:
                fm, body = parse_frontmatter(path.read_text())
            except (OSError, YamlSubsetError):
                continue
            about = ""
            for line in body.strip().splitlines():
                stripped = line.strip()
                if stripped:
                    about = stripped
                    break
            extends_raw = fm.get("extends")
            extends = (
                str(extends_raw) if isinstance(extends_raw, str)
                else ", ".join(str(x) for x in extends_raw) if isinstance(extends_raw, list)
                else ""
            )
            try:
                elems = bottle_elements([full_name], dirs)
            except Exception:  # pylint: disable=broad-exception-caught
                elems = []
            elem_summary = [f"{r['kind']}:{r['name']}" for r in elems[:10]]
            # What stacking it costs: every repo it grants, and its size.
            repos = [str(r.get("detail") or r["name"]) for r in elems if r["kind"] == "repo"]
            missing = library_facts(full_name, dirs, secrets)
            result.append({
                "name": full_name,
                "about": about,
                "extends": extends,
                "elements": elem_summary,
                "element_count": len(elems),
                "repos": repos,
                "missing_secrets": missing,
            })
    return result


def _forges(described: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The forges a grant may be on (ADR 0019): those this host connected,
    then those a bottle describes or the project is on but that aren't
    connected yet (`suggest.forges`), marked `connected: false`."""
    from ..forge_connections import ForgeConnectionError, connections
    try:
        found = connections()
    except ForgeConnectionError:
        found = {}
    out: list[dict[str, Any]] = [{"name": host, "kind": c.kind, "url": c.url, "connected": True}
                                 for host, c in sorted(found.items())]
    out += [{"name": host, "kind": str(d.get("kind") or "gitea"), "connected": False}
            for host, d in sorted(described.items()) if host not in found]
    return out


def _secret_names(host_root: Path) -> list[str]:
    """Names of secrets in the host secrets file (not their values)."""
    from ..secret_source import FileSecrets
    secrets_path = host_root / "secrets"
    try:
        fs = FileSecrets(secrets_path)
        raw = fs._load()  # pylint: disable=protected-access
        return sorted(raw.keys())
    except Exception:  # pylint: disable=broad-exception-caught
        return []


def _lockfile_names(cwd: Path) -> list[str]:
    """Common lockfile and manifest filenames present in `cwd`."""
    candidates = [
        "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
        "pyproject.toml", "requirements.txt", "Pipfile", "poetry.lock",
        "go.mod", "go.sum", "Cargo.toml", "Cargo.lock",
        "fly.toml", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
        ".tool-versions", "Makefile",
    ]
    return [f for f in candidates if (cwd / f).is_file()]


def _build_prompt(
    task: str,
    library: list[dict[str, Any]],
    project_meta: dict[str, Any],
    forges: list[dict[str, Any]],
    secret_names: list[str],
) -> str:
    sections = [
        f"Task: {task}",
        "",
        "Host bottle library (`missing_secrets`: secrets the bottle needs that this host "
        "doesn't have; a launch with it fails, so don't propose it):",
        json.dumps(library, indent=2),
        "",
        "Project metadata:",
        json.dumps(project_meta, indent=2),
        "",
        "Forges (a repo grant is `<name>/<owner>/<repo>[:write]` on one of these; it "
        "needs no bottle stacked for the forge). Grant on one with `connected: false` "
        "as on any other when the task needs it: the person is asked to connect it "
        "before the launch, so don't leave the repo out or explain it in a note:",
        json.dumps(forges, indent=2),
        "",
        "Secret names (values never shown):",
        json.dumps(secret_names, indent=2),
        "",
        "Respond with a single JSON object as described in your system prompt. Give it a "
        "\"summary\" key too: the gist for the person deciding, at most two short "
        "sentences and 120 characters, no markdown, shaped \"Builds on <bottles>. Adds "
        "<what> so the agent can <do what>.\" Good: \"Builds on bot-bottle-dev. Adds "
        "Linear so the agent can read tickets and label issues.\" Good: \"Builds on basic; "
        "it already covers this.\" Bad: \"Adds api.linear.app (GET, POST) and "
        "acme/app:write.\" Don't name hosts, methods or access: a line below lists them. "
        "Keep each "
        "addition's \"why\" to five words or fewer (\"fetch forecasts\"): it's shown in a "
        "column beside the host or repo.",
    ]
    return "\n".join(sections)


def _extract_json(text: str) -> str | None:
    """Extract the first JSON object from `text` (the model may prefix prose)."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _parse_and_repair(raw: str) -> Proposal | None:
    """Parse the model's text output into a Proposal, returning None if it
    can't be repaired."""
    extracted = _extract_json(raw)
    if not extracted:
        return None
    try:
        d = json.loads(extracted)
    except json.JSONDecodeError:
        return None
    if isinstance(d, dict):
        d.pop("forges", None)  # validation's to set, never the model's
    try:
        return Proposal.from_dict(d)
    except (ValueError, KeyError, TypeError):
        pass
    # Minimal repair: supply missing or invalid required fields.
    if isinstance(d, dict):
        from .proposal import OPTION_VALUES  # avoid top-level circular
        if d.get("option") not in OPTION_VALUES:
            d["option"] = "reuse"
        d.setdefault("bottles", [])
        d.setdefault("new_bottle", None)
        d.setdefault("additions", {})
        d.setdefault("similar", [])
        d.setdefault("notes", [])
        try:
            return Proposal.from_dict(d)
        except (ValueError, KeyError, TypeError):
            pass
    return None


def architect_suggest(
    task: str,
    cwd: Path | None,
    *,
    run: Runner,
    host_root: Path | None = None,
    described: dict[str, dict[str, Any]] | None = None,
) -> Proposal:
    """The host's `bottle-architect` agent's proposal for `task`, not yet
    validated. `cwd` is the project directory, or None when there's none to
    look at; `run` asks the architect (its warm bottle); `described` are the
    forges it may grant on that aren't connected (`suggest.forges`).
    ArchitectError when the architect can't answer."""
    if host_root is None:
        host_root = bot_bottle_root()
    architect = load_architect(host_root)

    host_bottles = host_root / "bottles"
    # pylint: disable-next=import-outside-toplevel
    from ..manifest.loader import BUILTIN_BOTTLES_DIR, PROJECT_BOTTLE, bottle_ref
    dirs: list[tuple[Path, bool]] = [(host_bottles, False)]
    if cwd is not None:
        dirs.append((cwd / ".bot-bottle" / "bottles", True))
    dirs.append((BUILTIN_BOTTLES_DIR, False))

    project_meta: dict[str, Any] = {"origin": None, "project_bottle": None, "lockfiles": []}
    if cwd is None:
        project_meta["note"] = "no project directory was given"
    else:
        project_meta["lockfiles"] = _lockfile_names(cwd)
        try:
            # pylint: disable-next=import-outside-toplevel
            from ..onboarding import read_origin
            origin = read_origin(cwd)
            if origin:
                project_meta["origin"] = f"https://{origin.host}/{origin.owner}/{origin.repo}"
                project_meta["project_bottle"] = bottle_ref(PROJECT_BOTTLE, True)
        except Exception:  # pylint: disable=broad-exception-caught
            pass

    library: list[dict[str, Any]] = []
    forges: list[dict[str, Any]] = []
    secret_names: list[str] = []
    try:
        library = _host_library(host_root, dirs)
        forges = _forges(described or {})
        secret_names = _secret_names(host_root)
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    prompt = _build_prompt(task, library, project_meta, forges, secret_names)
    raw = run(architect, prompt)
    proposal = _parse_and_repair(raw)
    if proposal is None:
        raise ArchitectError("its answer wasn't a proposal")
    return proposal


__all__ = ["TIMEOUT_SECONDS", "Architect", "ArchitectError", "Runner",
           "architect_suggest", "load_architect"]
