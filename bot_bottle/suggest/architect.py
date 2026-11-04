"""bottle-architect: the `bottle-architect` agent, run on the host with no
tools, produces a typed proposal (PRD 0117).

The agent is the file `setup` writes, `~/.bot-bottle/agents/bottle-architect.md`:
its body is the system prompt, `agent_provider.harness` picks the harness and
`model:` the model. Only the host's agent is read; a project can't replace it.

It runs as a print-mode call with no tools, no MCP servers and no settings
files (so no hooks), from an empty working directory and with a timeout, so
it can read nothing and do nothing but answer; the answer is parsed and then
validated against the host's bottles. A harness that can't run that way is
refused rather than run with tools. Only claude can, for now: `codex exec` is
an agent with a shell and other tools that can't all be turned off, and pi
has no verified tool-less mode.

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
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..paths import bot_bottle_root
from .proposal import Proposal
from .starter import ARCHITECT_AGENT

TIMEOUT_SECONDS = 120

# A model name goes on the harness's command line; keep it to a plain id.
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,99}")

# Why each harness that can't run with no tools is refused.
_REFUSED = {
    "codex": ("`codex exec` is an agent with a shell and other tools (apply_patch, "
              "web search, browser and computer use) that can't all be turned off, "
              "so it would run with tools on the host"),
    "pi": "pi has no verified way to run with no tools on the host",
}


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
    """The host's `bottle-architect` agent. ArchitectError when it's missing,
    doesn't load, or names a harness that can't run with no tools."""
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
    if harness not in _RUNNERS:
        why = _REFUSED.get(harness, "only a harness that runs with no tools can be used")
        raise ArchitectError(f"harness {harness!r} can't run as {ARCHITECT_AGENT}: "
                             f"{why}. Set `agent_provider: claude` in {path}")
    return Architect(path=path, prompt=agent.prompt, harness=harness, model=model)


def _host_library(host_root: Path, dirs: list[tuple[Path, bool]]) -> list[dict[str, Any]]:
    """Each bottle the host knows: name, about text (body first line), extends."""
    del host_root  # `dirs` already names where the host's bottles are
    from ..control.hostctl.manifest_elements import bottle_elements
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
                elem_summary = [f"{r['kind']}:{r['name']}" for r in elems[:10]]
            except Exception:  # pylint: disable=broad-exception-caught
                elem_summary = []
            result.append({
                "name": full_name,
                "about": about,
                "extends": extends,
                "elements": elem_summary,
            })
    return result


def _connected_forges(host_bottles: Path) -> list[dict[str, str]]:
    """Forge names and URLs from the host's bottle files."""
    from ..contract.yaml_subset import parse_frontmatter, YamlSubsetError
    result = []
    seen: set[str] = set()
    if not host_bottles.is_dir():
        return result
    for path in sorted(host_bottles.glob("*.md")):
        try:
            fm, _ = parse_frontmatter(path.read_text())
        except (OSError, YamlSubsetError):
            continue
        git = fm.get("git-gate")
        forges = git.get("forges") if isinstance(git, dict) else None
        if not isinstance(forges, dict):
            continue
        for key, entry in forges.items():
            if key in seen:
                continue
            seen.add(key)
            url = str(entry.get("url", "")) if isinstance(entry, dict) else ""
            result.append({"name": key, "url": url})
    return result


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
    forges: list[dict[str, str]],
    secret_names: list[str],
) -> str:
    sections = [
        f"Task: {task}",
        "",
        "Host bottle library:",
        json.dumps(library, indent=2),
        "",
        "Project metadata:",
        json.dumps(project_meta, indent=2),
        "",
        "Connected forges:",
        json.dumps(forges, indent=2),
        "",
        "Secret names (values never shown):",
        json.dumps(secret_names, indent=2),
        "",
        "Respond with a single JSON object as described in your system prompt.",
    ]
    return "\n".join(sections)


# Print mode with no built-in tools, no MCP servers, no settings files and
# no saved session.
CLAUDE_ARGS = ("--print", "--tools", "", "--strict-mcp-config", "--setting-sources", "",
               "--no-session-persistence")


def _run_claude(architect: Architect, prompt: str) -> str:
    """Run `claude` with no tools, from an empty directory, the prompt on
    stdin, and return its stdout. ArchitectError if it isn't installed,
    fails or times out."""
    claude = shutil.which("claude")
    if not claude:
        raise ArchitectError("claude isn't installed on this host")
    argv = [claude, *CLAUDE_ARGS, "--system-prompt", architect.prompt]
    if architect.model:
        argv += ["--model", architect.model]
    try:
        with tempfile.TemporaryDirectory(prefix="bottle-architect.") as empty:
            result = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                                    timeout=TIMEOUT_SECONDS, check=False, cwd=empty)
    except subprocess.TimeoutExpired as e:
        raise ArchitectError(f"claude didn't answer within {TIMEOUT_SECONDS}s") from e
    except OSError as e:
        raise ArchitectError(f"claude didn't run: {e}") from e
    if result.returncode != 0 or not result.stdout.strip():
        raise ArchitectError(f"claude failed (exit {result.returncode}); "
                             "is it logged in on this host?")
    return result.stdout.strip()


# The harnesses that can run with no tools, and how.
_RUNNERS = {"claude": _run_claude}


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
    host_root: Path | None = None,
) -> Proposal:
    """The host's `bottle-architect` agent's proposal for `task`, not yet
    validated. `cwd` is the project directory, or None when there's none to
    look at. ArchitectError when the architect can't answer."""
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
    forges: list[dict[str, str]] = []
    secret_names: list[str] = []
    try:
        library = _host_library(host_root, dirs)
        forges = _connected_forges(host_bottles)
        secret_names = _secret_names(host_root)
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    prompt = _build_prompt(task, library, project_meta, forges, secret_names)
    raw = _RUNNERS[architect.harness](architect, prompt)
    proposal = _parse_and_repair(raw)
    if proposal is None:
        raise ArchitectError("its answer wasn't a proposal")
    return proposal


__all__ = ["CLAUDE_ARGS", "TIMEOUT_SECONDS", "Architect", "ArchitectError",
           "architect_suggest", "load_architect"]
