"""Host-side primitives for Apple's `container` CLI."""

from __future__ import annotations

import json
import os
import ipaddress
import platform
import shutil
import subprocess
import tempfile
import time
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
    _require_container_service()


def _require_container_service() -> None:
    result = subprocess.run(
        [_CONTAINER, "system", "status"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        info("Apple Container system service is not running.")
        info("Start it with: container system start")
        die("container system service not running")


def dns_server() -> str:
    override = os.environ.get("BOT_BOTTLE_MACOS_CONTAINER_DNS", "").strip()
    if override:
        return override
    return _host_ipv4_dns() or _DEFAULT_DNS


def build_image(ref: str, context: str, *, dockerfile: str = "") -> None:
    """Build an OCI image with Apple's BuildKit-backed `container build`.

    Set `BOT_BOTTLE_NO_CACHE=1` (the `start --no-cache` flag) to force
    `--no-cache`. The npm/curl installers some provider Dockerfiles
    shell out to can silently no-op on a transient network failure —
    e.g. an `optionalDependencies` fetch for a platform-native binary —
    and the builder will then cache that broken layer indefinitely."""
    info(
        f"building image {ref} from {context} with Apple Container "
        "(layer cache keeps repeat builds fast)"
    )
    _ensure_builder_dns()
    args = [_CONTAINER, "build", "-t", ref, "--dns", dns_server()]
    if os.environ.get("BOT_BOTTLE_NO_CACHE") == "1":
        args.append("--no-cache")
    if dockerfile:
        # `container build` resolves -f relative to the current working
        # directory, not the build context. Anchor a relative Dockerfile to
        # the context so builds work from any cwd.
        if not os.path.isabs(dockerfile):
            dockerfile = os.path.join(context, dockerfile)
        args.extend(["-f", dockerfile])
    args.append(context)
    subprocess.run(args, check=True)


def verify_agent_image(image: str, argv: tuple[str, ...]) -> None:
    """Run `argv` inside a throwaway container of a freshly built agent
    image and die loudly if it fails, instead of shipping an image
    whose CLI only breaks at first real use. No-op when the provider
    hasn't declared a smoke test (`AgentProviderRuntime.smoke_test`)."""
    if not argv:
        return
    result = subprocess.run(
        [_CONTAINER, "run", "--rm", "--entrypoint", argv[0], image, *argv[1:]],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        die(
            f"agent image {image!r} failed its post-build smoke test "
            f"({' '.join(argv)}): {detail}\n"
            f"Try rebuilding from scratch: bot-bottle start --no-cache"
        )


def commit_container(container_name: str, image_tag: str) -> None:
    """Snapshot a running Apple Container as a local image.

    `container export` requires a stopped container, but Apple Container
    removes containers when they stop, making stop-then-export impossible.
    Instead, exec into the running container as root and stream the root
    filesystem out via tar, then build a new image from that archive.
    The bottle continues running after commit.
    """
    with tempfile.TemporaryDirectory(prefix="bot-bottle-container-commit.") as tmp:
        rootfs_tar = os.path.join(tmp, "rootfs.tar")
        dockerfile = os.path.join(tmp, "Dockerfile")
        with open(rootfs_tar, "wb") as tar_out:
            result = subprocess.run(
                [
                    _CONTAINER, "exec",
                    "--user", "root",
                    container_name,
                    "tar", "--create",
                    "--exclude=./proc",
                    "--exclude=./sys",
                    "--exclude=./dev",
                    "--exclude=./run",
                    "--file=-",
                    "--directory=/",
                    ".",
                ],
                stdout=tar_out,
                stderr=subprocess.PIPE,
                check=False,
            )
        if result.returncode != 0:
            die(
                f"container exec tar {container_name!r} failed: "
                f"{(result.stderr or b'').decode().strip() or '<no stderr>'}"
            )
        with open(dockerfile, "w", encoding="utf-8") as f:
            f.write(
                "FROM scratch\n"
                "ADD rootfs.tar /\n"
                "USER node\n"
                "WORKDIR /home/node\n"
            )
        build_image(image_tag, tmp, dockerfile=dockerfile)
    info(f"committed {container_name!r} → {image_tag!r}")


def _ensure_builder_dns() -> None:
    dns = dns_server()
    status = _builder_status()
    override = os.environ.get("BOT_BOTTLE_MACOS_CONTAINER_DNS", "").strip()
    if _builder_running(status) and _builder_resolves_build_hosts():
        if override and not _builder_has_dns(status, dns):
            _restart_builder_with_dns(dns)
        return
    _restart_builder_with_dns(dns)


def _restart_builder_with_dns(dns: str) -> None:
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


def _host_ipv4_dns() -> str:
    if not is_macos():
        return ""
    result = subprocess.run(
        ["scutil", "--dns"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("resolver #") and current:
            blocks.append(current)
            current = []
        current.append(line)
    if current:
        blocks.append(current)
    for direct_only in (True, False):
        for block in blocks:
            text = "\n".join(block)
            if direct_only and "Directly Reachable Address" not in text:
                continue
            for line in block:
                if "nameserver[" not in line or ":" not in line:
                    continue
                candidate = line.split(":", 1)[1].strip()
                if _usable_ipv4(candidate):
                    return candidate
    return ""


def _usable_ipv4(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (
        address.version == 4
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_unspecified
    )


def _builder_status() -> list[dict[str, object]]:
    result = subprocess.run(
        [_CONTAINER, "builder", "status", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [entry for entry in data if isinstance(entry, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _builder_running(status: list[dict[str, object]]) -> bool:
    for entry in status:
        entry_status = entry.get("status")
        if isinstance(entry_status, dict) and entry_status.get("state") == "running":
            return True
    return False


def _builder_dns_nameservers(status: list[dict[str, object]]) -> list[str]:
    out: list[str] = []
    for entry in status:
        config = entry.get("configuration")
        config_dns = config.get("dns") if isinstance(config, dict) else None
        nameservers = (
            config_dns.get("nameservers")
            if isinstance(config_dns, dict)
            else None
        )
        if not isinstance(nameservers, list):
            continue
        out.extend(name for name in nameservers if isinstance(name, str))
    return out


def _builder_has_dns(status: list[dict[str, object]], dns: str) -> bool:
    return dns in _builder_dns_nameservers(status)


def _builder_resolves_build_hosts() -> bool:
    result = subprocess.run(
        [_CONTAINER, "exec", "buildkit", "getent", "hosts", "deb.debian.org"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


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


def container_is_running(name: str) -> bool:
    """Return True if the named container is currently running.

    `container list` without `--all` lists only running containers."""
    result = subprocess.run(
        [_CONTAINER, "list", "--quiet"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    return name in {line.strip() for line in result.stdout.splitlines()}


def stop_container(name: str) -> None:
    """Stop the named container without deleting it."""
    result = subprocess.run(
        [_CONTAINER, "stop", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        die(
            f"container stop {name!r} failed: "
            f"{(result.stderr or '').strip() or '<no stderr>'}"
        )


def force_remove_container(name: str) -> None:
    if container_exists(name):
        subprocess.run(
            [_CONTAINER, "delete", "--force", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def copy_into_container(name: str, host_path: str, container_path: str) -> None:
    cmd = [_CONTAINER, "cp", host_path, f"{name}:{container_path}"]
    result = _run_container_op(cmd)
    if result.returncode != 0:
        die(
            f"container cp into {name}:{container_path} failed: "
            f"{(result.stderr or '').strip() or '<no stderr>'}"
        )


def exec_container(name: str, argv: list[str]) -> None:
    result = _run_container_op([_CONTAINER, "exec", name, *argv])
    if result.returncode != 0:
        die(
            f"container exec in {name} failed: "
            f"{(result.stderr or '').strip() or '<no stderr>'}"
        )


def _run_container_op(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    for _ in range(19):
        if result.returncode == 0:
            return result
        time.sleep(0.1)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    return result


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
    """The container's IPv4 address on `network`. Fatal if absent — callers
    that can tolerate "not yet" want `try_container_ipv4_on_network`."""
    ip = try_container_ipv4_on_network(name, network)
    if not ip:
        die(f"container {name} has no IPv4 address on {network}")
    return ip


def run_container_argv(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a `container` command, returning the result for the caller to
    interpret. Unlike the `die`-on-failure helpers above, this lets callers
    that raise their own typed errors (the gateway / orchestrator lifecycle)
    keep control of the failure path."""
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _normalize_digest(value: str) -> str:
    return value.split(":", 1)[1] if ":" in value else value


def image_digest(ref: str) -> str:
    """The digest of image `ref`, or "" if it can't be read. Empty is a
    'don't know' signal — callers treat it as "don't churn a working
    container" rather than as a mismatch."""
    result = run_container_argv([_CONTAINER, "image", "inspect", ref])
    if result.returncode != 0:
        return ""
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return ""
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return ""
    config = data.get("configuration")
    if isinstance(config, dict):
        descriptor = config.get("descriptor")
        if isinstance(descriptor, dict) and descriptor.get("digest"):
            return _normalize_digest(str(descriptor["digest"]))
    value = data.get("id")
    return _normalize_digest(str(value)) if value else ""


def container_image_digest(name: str) -> str:
    """The digest of the image container `name` was created from, or "" if it
    can't be read. Compare with `image_digest(ref)` to tell whether a running
    container predates an image rebuild."""
    result = run_container_argv([_CONTAINER, "inspect", name])
    if result.returncode != 0:
        return ""
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return ""
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return ""
    config = data.get("configuration")
    if not isinstance(config, dict):
        return ""
    image = config.get("image")
    if not isinstance(image, dict):
        return ""
    descriptor = image.get("descriptor")
    if isinstance(descriptor, dict) and descriptor.get("digest"):
        return _normalize_digest(str(descriptor["digest"]))
    return ""


def container_env(name: str) -> dict[str, str]:
    """The env container `name` was started with, or {} if unreadable. Lets a
    caller tell whether a running container's baked-in configuration still
    matches what it would pass today."""
    result = run_container_argv([_CONTAINER, "inspect", name])
    if result.returncode != 0:
        return {}
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return {}
    config = data.get("configuration")
    init = config.get("initProcess") if isinstance(config, dict) else None
    entries = init.get("environment") if isinstance(init, dict) else None
    if not isinstance(entries, list):
        return {}
    env: dict[str, str] = {}
    for entry in entries:
        if isinstance(entry, str) and "=" in entry:
            key, value = entry.split("=", 1)
            env[key] = value
    return env


def try_container_ipv4_on_network(name: str, network: str) -> str:
    """`container_ipv4_on_network` without the fatal exit: "" when the address
    isn't readable yet. For pollers — a container is created before it has an
    address, so "not yet" is an expected state there, not an error."""
    result = run_container_argv([_CONTAINER, "inspect", name])
    if result.returncode != 0:
        return ""
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return ""
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return ""
    status = data.get("status")
    networks = status.get("networks") if isinstance(status, dict) else None
    if not isinstance(networks, list):
        return ""
    for entry in networks:
        if not isinstance(entry, dict) or entry.get("network") != network:
            continue
        raw = entry.get("ipv4Address")
        if isinstance(raw, str) and raw:
            return raw.split("/", 1)[0]
    return ""


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
