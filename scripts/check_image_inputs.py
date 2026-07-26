#!/usr/bin/env python3
"""Fail when a supported image reintroduces mutable build inputs."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILES = (
    Path("Dockerfile.gateway"),
    Path("Dockerfile.orchestrator"),
    Path("Dockerfile.orchestrator.fc"),
    Path("bot_bottle/contrib/claude/Dockerfile"),
    Path("bot_bottle/contrib/codex/Dockerfile"),
    Path("bot_bottle/contrib/pi/Dockerfile"),
)
NPM_MANIFESTS = (
    Path("bot_bottle/contrib/claude/package.json"),
    Path("bot_bottle/contrib/pi/package.json"),
)
IMAGE_BUILD_ARGS = Path("image-build-args.json")
REQUIRED_BASE_ARGS = frozenset({
    "DOCKER_CLI_BASE_IMAGE",
    "NODE_BASE_IMAGE",
    "PYTHON_BASE_IMAGE",
})

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_EXACT_NPM_VERSION = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?$",
)
_SNAPSHOT = re.compile(r"ARG\s+DEBIAN_SNAPSHOT=\d{8}T\d{6}Z")
_NETWORK_TO_SHELL = re.compile(
    r"(?:curl|wget)\b[^|\n]*\|\s*(?:ba)?sh\b",
    re.IGNORECASE,
)


def _logical_lines(text: str) -> str:
    """Join Dockerfile continuations so policy regexes see one instruction."""
    return re.sub(r"\\\r?\n", " ", text)


def check_dockerfile(
    path: Path,
    text: str,
    image_build_args: dict[str, str] | None = None,
) -> list[str]:
    """Return policy violations for one Dockerfile."""
    problems: list[str] = []
    logical = _logical_lines(text)
    instructions = "\n".join(
        line for line in logical.splitlines()
        if not line.lstrip().startswith("#")
    )
    from_matches = list(re.finditer(r"(?im)^\s*FROM\s+(\S+)", logical))
    from_values = [match.group(1) for match in from_matches]
    preamble = logical[:from_matches[0].start()] if from_matches else logical
    base_args = {
        match.group(1): match.group(2)
        for match in re.finditer(
            r"(?im)^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)(?:=(\S+))?\s*$",
            preamble,
        )
    }
    if not from_values:
        problems.append(f"{path}: missing FROM")
    for value in from_values:
        variable = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
        if variable and variable.group(1) == "ORCHESTRATOR_BASE_IMAGE":
            if (
                "ORCHESTRATOR_BASE_IMAGE" not in base_args
                or base_args["ORCHESTRATOR_BASE_IMAGE"] is not None
            ):
                problems.append(
                    f"{path}: local base argument must have no mutable default",
                )
            continue
        if variable:
            name = variable.group(1)
            if name not in base_args:
                problems.append(
                    f"{path}: dynamic base argument {name} is not declared before FROM",
                )
                continue
            if base_args[name] is not None:
                problems.append(
                    f"{path}: base argument {name} must not define a default",
                )
                continue
            if image_build_args is None or name not in image_build_args:
                problems.append(
                    f"{path}: base argument {name} lacks a centralized value",
                )
                continue
            value = image_build_args[name]
        if "$" in value:
            problems.append(f"{path}: dynamic base image is not content-pinned: {value}")
            continue
        try:
            image, digest = value.rsplit("@sha256:", 1)
        except ValueError:
            problems.append(f"{path}: base image lacks a sha256 digest: {value}")
            continue
        leaf = image.rsplit("/", 1)[-1]
        if ":" not in leaf:
            problems.append(f"{path}: base image lacks a version-qualified tag: {value}")
        elif leaf.rsplit(":", 1)[1] == "latest":
            problems.append(f"{path}: base image uses latest: {value}")
        if not _DIGEST.fullmatch(digest):
            problems.append(f"{path}: base image digest is not 64 lowercase hex: {value}")

    if _NETWORK_TO_SHELL.search(instructions):
        problems.append(f"{path}: downloads network content directly into a shell")
    if re.search(r"\b(?:npm\s+(?:i|install)|pi\s+install)\b", instructions):
        problems.append(f"{path}: use npm ci with a committed integrity lock")
    if "apt-get install" in instructions:
        if not _SNAPSHOT.search(text):
            problems.append(f"{path}: apt install lacks a fixed Debian snapshot")
        if "snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}" not in text:
            problems.append(f"{path}: apt sources are not routed to the snapshot")
        if "Acquire::Check-Valid-Until=false" not in text:
            problems.append(f"{path}: snapshot apt update lacks validity override")
    if path == Path("Dockerfile.gateway"):
        if "--require-hashes" not in instructions:
            problems.append(f"{path}: gateway Python install does not require hashes")
        if "requirements.gateway.lock" not in text:
            problems.append(f"{path}: gateway Python lock is not consumed")
    if path == Path("bot_bottle/contrib/codex/Dockerfile"):
        required = (
            "codex-package_SHA256SUMS",
            "sha256sum -c",
            "codex-package-${codex_target}.tar.gz",
        )
        for marker in required:
            if marker not in instructions:
                problems.append(f"{path}: verified pinned Codex install lacks {marker!r}")
    return problems


def load_image_build_args(root: Path) -> tuple[dict[str, str], list[str]]:
    """Load and validate the one repository location for base-image args."""
    path = root / IMAGE_BUILD_ARGS
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"{IMAGE_BUILD_ARGS}: cannot read image build arguments: {exc}"]
    if not isinstance(data, dict):
        return {}, [f"{IMAGE_BUILD_ARGS}: must contain a JSON object"]
    problems: list[str] = []
    names = set(data)
    missing = REQUIRED_BASE_ARGS - names
    unexpected = names - REQUIRED_BASE_ARGS
    if missing:
        problems.append(
            f"{IMAGE_BUILD_ARGS}: missing arguments: {', '.join(sorted(missing))}",
        )
    if unexpected:
        problems.append(
            f"{IMAGE_BUILD_ARGS}: unexpected arguments: "
            f"{', '.join(sorted(unexpected))}",
        )
    for name, value in data.items():
        if not isinstance(value, str):
            problems.append(f"{IMAGE_BUILD_ARGS}: {name} must be a string")
    return {
        name: value for name, value in data.items()
        if isinstance(name, str) and isinstance(value, str)
    }, problems


def check_npm_manifest(root: Path, manifest_path: Path) -> list[str]:
    """Validate exact direct versions and integrity-complete npm locks."""
    problems: list[str] = []
    manifest_file = root / manifest_path
    lock_file = manifest_file.with_name("package-lock.json")
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        lock = json.loads(lock_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"{manifest_path}: cannot read manifest and lock: {exc}"]

    direct = manifest.get("dependencies", {})
    for package, version in direct.items():
        if not isinstance(version, str) or not _EXACT_NPM_VERSION.fullmatch(version):
            problems.append(
                f"{manifest_path}: {package} is not an exact version: {version!r}",
            )
    root_lock = lock.get("packages", {}).get("", {}).get("dependencies", {})
    if root_lock != direct:
        problems.append(f"{lock_file.relative_to(root)}: root dependencies are stale")
    if lock.get("lockfileVersion") != 3:
        problems.append(f"{lock_file.relative_to(root)}: require lockfileVersion 3")
    for package_path, package in lock.get("packages", {}).items():
        if not package_path or package.get("link"):
            continue
        resolved = package.get("resolved", "")
        if isinstance(resolved, str) and resolved.startswith(("http://", "https://")):
            integrity = package.get("integrity", "")
            if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
                problems.append(
                    f"{lock_file.relative_to(root)}: "
                    f"{package_path} lacks sha512 integrity",
                )
    return problems


def check_python_lock(root: Path) -> list[str]:
    """Validate that every gateway Python requirement is exact and hashed."""
    path = root / "requirements.gateway.lock"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"requirements.gateway.lock: cannot read lock: {exc}"]
    problems: list[str] = []
    blocks = re.split(r"(?m)(?=^[A-Za-z0-9_.-]+==)", text)
    requirements = [block for block in blocks if re.match(r"^[A-Za-z0-9_.-]+==", block)]
    if not requirements:
        problems.append("requirements.gateway.lock: contains no exact requirements")
    for block in requirements:
        name = block.split("==", 1)[0]
        first_line = block.splitlines()[0]
        if not re.match(r"^[A-Za-z0-9_.-]+==[^\s\\]+", first_line):
            problems.append(f"requirements.gateway.lock: {name} is not exact")
        if not re.search(r"--hash=sha256:[0-9a-f]{64}", block):
            problems.append(f"requirements.gateway.lock: {name} lacks a sha256 hash")
    try:
        direct = [
            line.strip()
            for line in (root / "requirements.gateway.in").read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    except OSError as exc:
        problems.append(f"requirements.gateway.in: cannot read input: {exc}")
        direct = []
    for requirement in direct:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s]+", requirement):
            problems.append(
                f"requirements.gateway.in: direct dependency is not exact: {requirement}",
            )
        if not re.search(rf"(?m)^{re.escape(requirement)}(?:\s|\\)", text):
            problems.append(
                f"requirements.gateway.lock: missing direct dependency {requirement}",
            )
    return problems


def check_repo(root: Path = REPO_ROOT) -> list[str]:
    """Return every repository image-input policy violation."""
    image_build_args, problems = load_image_build_args(root)
    for path in DOCKERFILES:
        try:
            text = (root / path).read_text(encoding="utf-8")
        except OSError as exc:
            problems.append(f"{path}: cannot read Dockerfile: {exc}")
            continue
        problems.extend(check_dockerfile(path, text, image_build_args))
    for manifest in NPM_MANIFESTS:
        problems.extend(check_npm_manifest(root, manifest))
    nested_path = Path(
        "bot_bottle/backend/macos_container/nested_containers.py",
    )
    try:
        nested_source = (root / nested_path).read_text(encoding="utf-8")
    except OSError as exc:
        problems.append(f"{nested_path}: cannot read generated image source: {exc}")
    else:
        if "FROM docker:" in nested_source:
            problems.append(
                "nested-containers image uses a mutable Docker CLI base",
            )
        if '"ARG DOCKER_CLI_BASE_IMAGE\\n"' not in nested_source:
            problems.append(
                "nested-containers image does not consume DOCKER_CLI_BASE_IMAGE",
            )
        if "pin_local_base(base_image)" not in nested_source:
            problems.append(
                "nested-containers image does not pin its local agent base",
            )
    problems.extend(check_python_lock(root))
    return problems


def main() -> int:
    problems = check_repo()
    if problems:
        for problem in problems:
            print(f"image-input policy: {problem}", file=sys.stderr)
        return 1
    print("image-input policy: all supported image inputs are pinned and verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
