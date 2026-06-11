"""Host-side primitives for Apple's `container` CLI."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from typing import Iterable

from ...log import die, info


_CONTAINER = "container"
_DEFAULT_DNS = "1.1.1.1"


def is_macos() -> bool:
    return platform.system() == "Darwin"


def is_available() -> bool:
    return is_macos() and shutil.which(_CONTAINER) is not None


def require_container() -> None:
    """Fail with an install pointer if Apple Container is unavailable."""
    if not is_macos():
        info("BOT_BOTTLE_BACKEND=macos-container requires macOS.")
        die("macos-container backend is only supported on macOS")
    if shutil.which(_CONTAINER) is None:
        info("Apple Container is required but was not found on PATH.")
        info("Install: https://github.com/apple/container/releases")
        die("container not found")


def dns_server() -> str:
    return os.environ.get("BOT_BOTTLE_MACOS_CONTAINER_DNS", _DEFAULT_DNS)


def build_image(ref: str, context: str, *, dockerfile: str = "") -> None:
    """Build an OCI image with Apple's BuildKit-backed `container build`."""
    info(
        f"building image {ref} from {context} with Apple Container "
        "(layer cache keeps repeat builds fast)"
    )
    _ensure_builder_dns()
    args = [_CONTAINER, "build", "-t", ref, "--dns", dns_server()]
    if dockerfile:
        args.extend(["-f", dockerfile])
    args.append(context)
    subprocess.run(args, check=True)


def _ensure_builder_dns() -> None:
    dns = dns_server()
    if _builder_has_dns(dns):
        return
    subprocess.run(
        [_CONTAINER, "builder", "stop"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    subprocess.run(
        [_CONTAINER, "builder", "start", "--dns", dns],
        check=True,
    )


def _builder_has_dns(dns: str) -> bool:
    result = subprocess.run(
        [_CONTAINER, "builder", "status", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return False
    entries = data if isinstance(data, list) else [data]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        if isinstance(status, dict) and status.get("state") != "running":
            continue
        config = entry.get("configuration")
        config_dns = config.get("dns") if isinstance(config, dict) else None
        nameservers = (
            config_dns.get("nameservers")
            if isinstance(config_dns, dict)
            else None
        )
        if isinstance(nameservers, list) and dns in nameservers:
            return True
    return False


def image_exists(ref: str) -> bool:
    return _silent_run([_CONTAINER, "image", "inspect", ref]) == 0


def container_exists(name: str) -> bool:
    result = subprocess.run(
        [_CONTAINER, "list", "--all", "--quiet"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    return name in {line.strip() for line in result.stdout.splitlines()}


def force_remove_container(name: str) -> None:
    if container_exists(name):
        subprocess.run(
            [_CONTAINER, "delete", "--force", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def create_network(name: str, *, internal: bool = False) -> None:
    args = [
        _CONTAINER, "network", "create",
        "--label", "bot-bottle.backend=macos-container",
    ]
    if internal:
        args.append("--internal")
    args.append(name)
    result = subprocess.run(
        args, capture_output=True, text=True, check=False,
    )
    if result.returncode == 0:
        return
    if "already exists" in (result.stderr or "").lower():
        return
    die(
        f"container network create {name} failed: "
        f"{(result.stderr or '').strip() or '<no stderr>'}"
    )


def remove_network(name: str) -> None:
    result = subprocess.run(
        [_CONTAINER, "network", "delete", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return


def inspect_container(name: str) -> dict[str, object]:
    result = subprocess.run(
        [_CONTAINER, "inspect", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        die(
            f"container inspect {name} failed: "
            f"{(result.stderr or '').strip() or '<no stderr>'}"
        )
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        die(f"container inspect {name} returned malformed JSON: {exc}")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict):
        return data
    die(f"container inspect {name} returned an unexpected shape")
    raise AssertionError("unreachable")


def container_ipv4_on_network(name: str, network: str) -> str:
    data = inspect_container(name)
    status = data.get("status")
    networks = status.get("networks") if isinstance(status, dict) else None
    if not isinstance(networks, list):
        die(f"container inspect {name} did not include status.networks")
    for entry in networks:
        if not isinstance(entry, dict):
            continue
        if entry.get("network") != network:
            continue
        raw = entry.get("ipv4Address")
        if not isinstance(raw, str) or not raw:
            die(f"container {name} has no IPv4 address on {network}")
        return raw.split("/", 1)[0]
    die(f"container {name} is not attached to network {network}")
    raise AssertionError("unreachable")


def image_id(ref: str) -> str:
    """Return the image digest/ID from `container image inspect`.

    The command returns JSON on current Apple Container releases. Keep
    parsing narrow and fatal so callers do not cache on an empty key.
    """
    import json

    result = subprocess.run(
        [_CONTAINER, "image", "inspect", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        die(
            f"container image inspect for {ref!r} failed: "
            f"{(result.stderr or '').strip() or '<no stderr>'}"
        )
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        die(f"container image inspect for {ref!r} returned malformed JSON: {exc}")
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        value = data.get("id") or data.get("digest") or data.get("ID")
        if value:
            return str(value)
    die(f"container image inspect for {ref!r} did not include an image id")
    raise AssertionError("unreachable")


def save(ref: str, output: str) -> None:
    subprocess.run([_CONTAINER, "image", "save", ref, "-o", output], check=True)


def _silent_run(cmd: Iterable[str]) -> int:
    return subprocess.run(
        list(cmd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode
