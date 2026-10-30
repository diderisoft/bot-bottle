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
from datetime import datetime, timezone
from typing import Iterable

from ... import resources
from ...lib.proc import run_streaming
from ...lib.log import die, info


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


def service_running() -> bool:
    """True iff Apple Container's system service is up (otherwise nothing
    runs)."""
    if shutil.which(_CONTAINER) is None:
        return False
    return subprocess.run(
        [_CONTAINER, "system", "status"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    ).returncode == 0


def _require_container_service() -> None:
    if not service_running():
        info("Apple Container system service is not running.")
        info("Start it with: container system start")
        die("container system service not running")


def dns_server(override: str = "") -> str:
    """`override` (`InfraSettings.macos_container_dns`), else the host's IPv4
    resolver, else a public default."""
    if override:
        return override
    return _host_ipv4_dns() or _DEFAULT_DNS


def build_image(
    ref: str,
    context: str,
    *,
    dockerfile: str = "",
    build_args: dict[str, str] | None = None,
    no_cache: bool = False,
    dns_override: str = "",
) -> None:
    """Build with Apple's `container build`, resolving through
    `dns_server(dns_override)`. `no_cache` forces `--no-cache`, for layers a
    flaky installer cached broken."""
    info(
        f"building image {ref} from {context} with Apple Container "
        "(layer cache keeps repeat builds fast)"
    )
    _ensure_builder_dns(dns_override)
    args = [_CONTAINER, "build", "-t", ref, "--dns", dns_server(dns_override)]
    if no_cache:
        args.append("--no-cache")
    if dockerfile:
        # `container build` resolves -f against the cwd, not the context.
        if not os.path.isabs(dockerfile):
            dockerfile = os.path.join(context, dockerfile)
        args.extend(["-f", dockerfile])
    effective_build_args = resources.image_build_args(
        dockerfile,
        context=context,
    ) if dockerfile else {}
    effective_build_args.update(build_args or {})
    for name, value in effective_build_args.items():
        args.extend(["--build-arg", f"{name}={value}"])
    args.append(context)
    # Streamed so a host-controller job captures the output.
    run_streaming(args, check=True)


def verify_agent_image(image: str, argv: tuple[str, ...]) -> None:
    """Run `argv` in a throwaway container of a fresh image and die if it
    fails. No-op without a provider smoke test."""
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


def commit_container(
    container_name: str, image_tag: str, *, no_cache: bool = False, dns_override: str = "",
) -> None:
    """Snapshot a running container as a local image. Apple removes stopped
    containers, so the root filesystem is streamed out with tar and built into
    an image. The bottle keeps running.
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
        build_image(image_tag, tmp, dockerfile=dockerfile, no_cache=no_cache,
                    dns_override=dns_override)
    info(f"committed {container_name!r} → {image_tag!r}")


def _ensure_builder_dns(override: str) -> None:
    dns = dns_server(override)
    status = _builder_status()
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


def tag_image(src: str, dst: str) -> None:
    """Tag local image `src` as `dst` too."""
    result = subprocess.run(
        [_CONTAINER, "image", "tag", src, dst],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        die(f"could not tag {src} as {dst}: {detail or '<no detail>'}")


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
    """True if the named container is running."""
    result = subprocess.run(
        [_CONTAINER, "list", "--quiet"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    return name in {line.strip() for line in result.stdout.splitlines()}


def list_volumes() -> list[str]:
    """Every volume's name. Raises EnumerationError when they can't be listed."""
    # pylint: disable-next=import-outside-toplevel
    from .. import EnumerationError
    result = subprocess.run([_CONTAINER, "volume", "list", "--quiet"],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise EnumerationError(f"container volume list failed: "
                               f"{result.stderr.strip() or f'exit {result.returncode}'}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def volume_exists(name: str) -> bool:
    try:
        return name in list_volumes()
    except Exception:  # pylint: disable=broad-exception-caught  # treat as absent
        return False


def remove_volume(name: str) -> None:
    """Delete volume `name` if it's there. Raises if it can't be."""
    if not volume_exists(name):
        return
    result = subprocess.run([_CONTAINER, "volume", "delete", name],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"container volume delete {name}: "
                           f"{result.stderr.strip() or f'exit {result.returncode}'}")


def stop_container(name: str) -> None:
    """Stop container `name`, keeping it. Best effort. Its init is `sleep`,
    which ignores SIGTERM, and the agent has ended by then: a short wait."""
    subprocess.run([_CONTAINER, "stop", "--time", "2", name], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=False)


def start_container(name: str) -> str:
    """Start the stopped container `name`: "" when it started, else why not."""
    result = subprocess.run([_CONTAINER, "start", name], capture_output=True, text=True,
                            check=False)
    return "" if result.returncode == 0 else (result.stderr.strip() or f"exit {result.returncode}")


def force_remove_container(name: str) -> None:
    if container_exists(name):
        subprocess.run(
            [_CONTAINER, "delete", "--force", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def exec_container_as_root(name: str, argv: list[str]) -> None:
    """Run `argv` in container `name` as uid 0, for host maintenance the agent mustn't be
    able to do (e.g. rewriting `/etc/hosts`).
    """
    result = _run_container_op([_CONTAINER, "exec", "--user", "root", name, *argv])
    if result.returncode != 0:
        die(
            f"container exec (root) in {name} failed: "
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


def create_network(
    name: str, *, internal: bool = False, subnet: str = "", subnet_v6: str = "",
) -> None:
    args = [
        _CONTAINER, "network", "create",
        "--label", "bot-bottle.backend=macos-container",
    ]
    if internal:
        args.append("--internal")
    if subnet:
        args += ["--subnet", subnet]
    if subnet_v6:
        args += ["--subnet-v6", subnet_v6]
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


def run_container_argv(
    argv: list[str], *, env: dict[str, str] | None = None, timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a `container` command and return the result, for callers with their
    own error types. `env` is the child env, for a bare `--env NAME` secret.
    `timeout` raises `subprocess.TimeoutExpired`."""
    return subprocess.run(
        argv, capture_output=True, text=True, check=False, env=env, timeout=timeout)


def bind_mount_spec(source: str, target: str, *, readonly: bool = False) -> str:
    """A `container run --mount` bind spec, shared so the gateway and
    orchestrator agree."""
    spec = f"type=bind,source={source},target={target}"
    if readonly:
        spec += ",readonly"
    return spec


def _normalize_digest(value: str) -> str:
    return value.split(":", 1)[1] if ":" in value else value


def _inspect_first(argv: list[str]) -> dict[str, object]:
    """An inspect command's first JSON object, or {} on any failure. {} means
    "don't know": leave the container alone."""
    result = run_container_argv(argv)
    if result.returncode != 0:
        return {}
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    if isinstance(data, list):
        data = data[0] if data else {}
    return data if isinstance(data, dict) else {}


def _descriptor_digest(node: object) -> str:
    """The digest under a `{"descriptor": {"digest": ...}}` node, or "". Image
    and container inspect share the shape."""
    if not isinstance(node, dict):
        return ""
    descriptor = node.get("descriptor")
    if isinstance(descriptor, dict) and descriptor.get("digest"):
        return _normalize_digest(str(descriptor["digest"]))
    return ""


def image_digest(ref: str) -> str:
    """Digest of image `ref` (`configuration.descriptor.digest`), or ""."""
    data = _inspect_first([_CONTAINER, "image", "inspect", ref])
    return _descriptor_digest(data.get("configuration"))


def container_image_digest(name: str) -> str:
    """Digest of the image container `name` was created from, or ""."""
    config = _inspect_first([_CONTAINER, "inspect", name]).get("configuration")
    image = config.get("image") if isinstance(config, dict) else None
    return _descriptor_digest(image)


def container_env(name: str) -> dict[str, str]:
    """The env container `name` was started with, or {}."""
    config = _inspect_first([_CONTAINER, "inspect", name]).get("configuration")
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
    """The container's IPv4 on `network`, or "" when it has none yet."""
    status = _inspect_first([_CONTAINER, "inspect", name]).get("status")
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


def network_ipv4(network: str) -> tuple[str, str] | None:
    """(host gateway address, IPv4 subnet) of `network`, or None."""
    status = _inspect_first([_CONTAINER, "network", "inspect", network]).get("status")
    if not isinstance(status, dict):
        return None
    gateway, subnet = status.get("ipv4Gateway"), status.get("ipv4Subnet")
    try:
        if not ipaddress.ip_address(str(gateway)) in ipaddress.ip_network(str(subnet)):
            return None
    except ValueError:
        return None
    return str(gateway), str(subnet)


def network_subnets(network: str) -> tuple[str, str] | None:
    """(IPv4 subnet, IPv6 subnet) of `network`, normalized, or None."""
    status = _inspect_first([_CONTAINER, "network", "inspect", network]).get("status")
    if not isinstance(status, dict):
        return None
    try:
        # Status gives the IPv6 prefix as the host's address on it
        # (`fd62:…::1/64`), so host bits are allowed.
        v4 = ipaddress.ip_network(str(status.get("ipv4Subnet")), strict=False)
        v6 = ipaddress.ip_network(str(status.get("ipv6Subnet")), strict=False)
    except ValueError:
        return None
    return str(v4), str(v6)


def inspect_container_network_ip(name: str, network: str) -> str | None:
    """IP of `name` on `network`: the IP, "" when none is assigned yet, or None
    when inspect fails.
    """
    result = subprocess.run(
        [_CONTAINER, "inspect", name],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return None
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


def wait_container_ipv4_on_network(
    name: str, network: str, *, timeout: float = 15.0, poll: float = 0.25,
) -> str:
    """Poll for the container's DHCP address on `network`; "" on timeout. `run
    --detach` can return before vmnet assigns it."""
    deadline = time.monotonic() + timeout
    while True:
        ip = try_container_ipv4_on_network(name, network)
        if ip:
            return ip
        if time.monotonic() >= deadline:
            return ""
        time.sleep(poll)


def image_id(ref: str) -> str:
    """The image ID from `container image inspect`. Fatal on failure, so
    callers never cache on an empty key.
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


def pinned_local_image_ref(ref: str) -> str:
    """Tag a local image with its full content ID for a stable `FROM`, so a
    concurrent build can't move `:latest` in between. Apple's `image tag`
    rejects a bare ID, so the mutable `ref` is tagged and then checked to
    resolve to the expected ID.
    """
    image = image_id(ref)
    digest = image.removeprefix("sha256:")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        die(f"could not derive a local base tag from invalid image ID {image!r}")
    repository = ref.split("@", 1)[0]
    last_slash = repository.rfind("/")
    last_colon = repository.rfind(":")
    if last_colon > last_slash:
        repository = repository[:last_colon]
    pinned_ref = f"{repository}:sha256-{digest}"
    result = subprocess.run(
        [_CONTAINER, "image", "tag", ref, pinned_ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        die(
            f"could not tag exact local image {image} via {ref!r}: "
            f"{(result.stderr or result.stdout or '').strip() or '<no detail>'}"
        )
    if image_id(pinned_ref) != image:
        die(f"content-derived local tag {pinned_ref!r} did not resolve to {image}")
    return pinned_ref


def image_created_at(ref: str) -> datetime | None:
    """The image creation time as aware UTC, or None (skip the stale check)."""
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
        value = data.get("created") or data.get("Created")
        if isinstance(value, str) and value:
            try:
                ts = value.rstrip("Z")
                return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    return None


def save(ref: str, output: str) -> None:
    subprocess.run([_CONTAINER, "image", "save", ref, "-o", output], check=True)


def _silent_run(cmd: Iterable[str]) -> int:
    return subprocess.run(
        list(cmd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode
