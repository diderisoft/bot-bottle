"""bottle-architect: run the user's own harness in print mode with no tools
to produce a typed proposal JSON (PRD 0117, chunk 2).

Print mode (no interactive session, no sandbox) means the harness CLI is
run directly on the host with its own login.  The output is a JSON object
matching the Proposal schema.  Invalid JSON or a schema error falls back to
the rules path.

Harness precedence: claude first, then codex.  When neither is logged in the
rules path is used.

Input to the model: the task, the host's bottle library (names, about text,
extends, elements), project metadata (origin URL, the project bottle's name,
and lockfile/manifest *file names* — never their contents), connected forges,
and secret names.  The model never sees secret values or file contents.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..paths import bot_bottle_root
from .proposal import Proposal
from .rules import rules_suggest
from .validate import validate_proposal

# The agent manifest template written by `setup`.  Kept here so `setup` can
# import and write it without importing the whole suggest package at startup.
ARCHITECT_MANIFEST = """\
---
# bottle-architect: suggest a bottle for a task (PRD 0117).
# Written by bot-bottle setup.  Edit the prompt to tune the output.
agent_provider:
  harness: claude
  forward_host_credentials: true
---

You are bottle-architect, a tool that proposes a bot-bottle bottle for a task.
Your output must be a single JSON object matching this schema exactly — no
prose before or after it:

{
  "option": "<reuse|stack|stack+additions|new-extending|new>",
  "bottles": ["<existing-bottle-name>", ...],
  "new_bottle": null | {"name": "...", "extends": "...", "about": "..."},
  "additions": {
    "egress":      [{"host": "...", "methods": ["GET","POST",...], "why": "..."}],
    "repos":       [{"grant": "<forge>/<owner>/<repo>:write", "why": "..."}],
    "env":         [{"name": "ENV_VAR_NAME", "why": "..."}],
    "credentials": [{"name": "SECRET_NAME", "route": "api.example.com", "why": "..."}]
  },
  "similar": [{"bottle": "...", "covers": 0.9}],
  "notes": ["..."]
}

Rules:
- Prefer reuse > stack > stack+additions > new-extending > new.
- Bias toward too little access; leave out anything uncertain.
- Never propose inspect:false, never include secret values.
- Grants only on connected forges listed in the input.
- Credentials name a secret from the secret names list and the egress route
  they are injected on (the route must also be in additions.egress).
- env names only; the value comes from the secrets file at launch.
"""


def _host_library(host_root: Path, dirs: list[tuple[Path, bool]]) -> list[dict[str, Any]]:
    """Each bottle the host knows: name, about text (body first line), extends."""
    from ..control.hostctl.manifest_elements import _chain_files, bottle_elements
    from ..manifest.loader import scan_bottle_names, BUILTIN_BOTTLES_DIR
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


def _try_claude(prompt: str, system_prompt: str) -> str | None:
    """Run `claude --print` with a piped prompt; return stdout or None."""
    claude = shutil.which("claude")
    if not claude:
        return None
    try:
        result = subprocess.run(
            [claude, "--print", "--no-markdown",
             "--system-prompt", system_prompt,
             "--no-tools"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _try_codex(prompt: str, system_prompt: str) -> str | None:
    """Run `codex` in non-interactive mode; return stdout or None."""
    codex = shutil.which("codex")
    if not codex:
        return None
    full_prompt = f"{system_prompt}\n\n{prompt}"
    try:
        result = subprocess.run(
            [codex, "--quiet", full_prompt],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


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
    cwd: Path | None = None,
    *,
    host_root: Path | None = None,
) -> Proposal:
    """Run `bottle-architect` in print mode; fall back to rules if no harness is
    logged in or the output can't be parsed and validated.

    `cwd` defaults to the current directory.  `host_root` defaults to
    `bot_bottle_root()`.
    """
    if cwd is None:
        cwd = Path(os.getcwd())
    if host_root is None:
        host_root = bot_bottle_root()

    host_bottles = host_root / "bottles"
    from ..manifest.loader import BUILTIN_BOTTLES_DIR
    dirs: list[tuple[Path, bool]] = [
        (host_bottles, False),
        (cwd / ".bot-bottle" / "bottles", True),
        (BUILTIN_BOTTLES_DIR, False),
    ]

    from ..manifest.loader import PROJECT_BOTTLE, bottle_ref
    project_meta: dict[str, Any] = {
        "origin": None,
        "project_bottle": None,
        "lockfiles": _lockfile_names(cwd),
    }
    try:
        from ..onboarding import read_origin
        origin = read_origin(cwd)
        if origin:
            project_meta["origin"] = f"https://{origin.host}/{origin.owner}/{origin.repo}"
            project_meta["project_bottle"] = bottle_ref(PROJECT_BOTTLE, True)
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    library = []
    forges: list[dict[str, str]] = []
    secret_names: list[str] = []
    try:
        library = _host_library(host_root, dirs)
        forges = _connected_forges(host_bottles)
        secret_names = _secret_names(host_root)
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    prompt = _build_prompt(task, library, project_meta, forges, secret_names)
    system_prompt = ARCHITECT_MANIFEST.split("---\n", 2)[-1].strip()

    raw: str | None = _try_claude(prompt, system_prompt)
    if raw is None:
        raw = _try_codex(prompt, system_prompt)
    if raw is None:
        return rules_suggest(task, cwd, host_root=host_root)

    proposal = _parse_and_repair(raw)
    if proposal is None:
        proposal = rules_suggest(task, cwd, host_root=host_root)
        proposal.notes.append("architect output could not be parsed; fell back to rules")
        return proposal

    validated, _notes = validate_proposal(proposal, dirs)
    return validated


__all__ = ["ARCHITECT_MANIFEST", "architect_suggest"]
