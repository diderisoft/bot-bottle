"""Every `BOT_BOTTLE_*` environment variable bot-bottle reads or sets, in one
place. Metadata only: the code that reads a variable still reads it where it
always has (host behaviour knobs through `cli/settings.py`).

`scripts/gen_env_docs.py` renders the tables in `docs/reference/install.md`
(user and dev) and `docs/reference/runtime-contract.md` (internal) from this;
`tests/unit/test_env_vars.py` fails when a `BOT_BOTTLE_*` name in the package
is missing here, an entry here is no longer used, or the docs are stale.

Audiences:
- `user`: a knob for whoever installs or runs bot-bottle.
- `dev`: for working on bot-bottle itself (its own CI, local image builds).
- `release`: publishing the infra images.
- `internal`: set by bot-bottle for its own processes; never set by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Audience = Literal["user", "dev", "release", "internal"]

AUDIENCES: tuple[Audience, ...] = ("user", "dev", "release", "internal")

# Shown in a variable's description when it is due to go.
GITHUB_CUTOVER = "Removed at the GitHub cutover."


@dataclass(frozen=True)
class EnvVar:
    name: str
    audience: Audience
    description: str
    default: str = ""


ENV_VARS: tuple[EnvVar, ...] = (
    # --- user: running bot-bottle ---
    EnvVar("BOT_BOTTLE_BACKEND", "user",
           "Backend to use: `macos-container`, `firecracker` or `docker`. "
           "Same as `--backend`.",
           "the host default"),
    EnvVar("BOT_BOTTLE_ENV", "user",
           "The environment: `live`, or a name (lowercase letters, digits and `-`, "
           "at most 24) such as `dev` or `ci-<run>`. A named one gets its own state "
           "root, `bb-<name>-*` container, VM, network and volume names, docker "
           "subnets and firecracker pool slice, so it never touches the live "
           "install; explicit settings below still win. See `bot-bottle envs`.",
           "`live`"),
    EnvVar("BOT_BOTTLE_ROOT", "user",
           "State root: manifests, keys, secrets, the host DB and audit journal.",
           "`~/.bot-bottle`; `~/.bot-bottle-envs/<name>` in a named environment"),
    EnvVar("BOT_BOTTLE_LOG_LEVEL", "user",
           "`debug`, `info`, `warn` or `error`. `debug` also turns on the "
           "orchestrator's access log.",
           "`info`"),
    EnvVar("BOT_BOTTLE_INFRA_BUILD", "user",
           "`local` builds the orchestrator and gateway images from source "
           "instead of pulling a release's, for every command; `bot-bottle up "
           "--build` is the flag form. Building is never automatic: an install "
           "with no published images that match (e.g. from `main`) stops "
           "without it.",
           "pull"),
    EnvVar("BOT_BOTTLE_BUILD_ENGINE", "user",
           "Image build engine: `docker`, `podman` or `vm`.",
           "the first usable one"),
    EnvVar("BOT_BOTTLE_ARTIFACT_CACHE", "user",
           "Where verified artifact downloads (infra rootfs, image layers) are cached.",
           "`~/.cache/bot-bottle/artifacts`"),
    EnvVar("BOT_BOTTLE_INFRA_ARTIFACT_TOKEN", "user",
           "Token for pulling from a private image registry (sent only to "
           "`BOT_BOTTLE_INFRA_ARTIFACT_TOKEN_HOST`), or with `write:packages` "
           "publishing to the release registry. Published images are public, so "
           "pulling them needs none."),
    EnvVar("BOT_BOTTLE_INFRA_ARTIFACT_TOKEN_HOST", "user",
           "The one registry host `BOT_BOTTLE_INFRA_ARTIFACT_TOKEN` is sent to when "
           "pulling. Unset, it is sent nowhere.",
           "unset"),
    EnvVar("BOT_BOTTLE_ORCHESTRATOR_TEARDOWN_TIMEOUT_SECONDS", "user",
           "How long a bottle's teardown waits on the orchestrator. Read by the "
           "process that runs the launch (the host controller), so set it in "
           "that process's environment.",
           "`30`"),
    EnvVar("BOT_BOTTLE_DOCKER_GATEWAY_SUBNET", "user",
           "docker: the gateway network's subnet.",
           "`10.242.255.0/24`; a named environment allocates a free /24 of "
           "`10.241.0.0/16` and records it"),
    EnvVar("BOT_BOTTLE_DOCKER_AGENT_SUBNETS", "user",
           "docker: the range per-agent networks are carved from, as /29s.",
           "`10.242.0.0/20`; `10.240.0.0/16` in a named environment"),
    EnvVar("BOT_BOTTLE_MACOS_CONTAINER_DNS", "user",
           "macos-container: DNS server for the gateway, orchestrator and image "
           "builder. When set, a builder running with other DNS is restarted with it.",
           "the host's IPv4 resolver, else `1.1.1.1`"),
    EnvVar("BOT_BOTTLE_FC_CACHE", "user",
           "firecracker: cache root for the kernel, root filesystems and run dirs.",
           "`~/.cache/bot-bottle/firecracker`"),
    EnvVar("BOT_BOTTLE_FC_KERNEL", "user",
           "firecracker: the guest kernel.",
           "`vmlinux` in the firecracker cache"),
    EnvVar("BOT_BOTTLE_FC_DROPBEAR", "user",
           "firecracker: the static dropbear put in every guest as its SSH server.",
           "`dropbear` in the firecracker cache"),
    EnvVar("BOT_BOTTLE_FC_POOL_SIZE", "user",
           "firecracker: TAP pool slots, so concurrent bottles. Must match the "
           "host's network pool.",
           "`8`"),
    EnvVar("BOT_BOTTLE_FC_ENV_SLOTS", "user",
           "firecracker: network-pool slices the host reserves for named "
           "environments, after the live pool. Must be at most the host's.",
           "`4`"),
    EnvVar("BOT_BOTTLE_FC_SLICES_DIR", "user",
           "firecracker: the host-wide claims on those slices, created by the "
           "network pool setup for the pool's group. Must match the host's.",
           "`/var/lib/bot-bottle-fc/slices`"),
    EnvVar("BOT_BOTTLE_FC_IP_BASE", "user",
           "firecracker: base IPv4 of the pool's /31s. Must match the host's "
           "network pool.",
           "`10.243.0.0`; `10.243.<k>.0` for a named environment's slice k"),
    EnvVar("BOT_BOTTLE_FC_IFACE_PREFIX", "user",
           "firecracker: agent TAP name prefix. Must match the host's network pool.",
           "`bbfc`; `bb<k>fc` for slice k"),
    EnvVar("BOT_BOTTLE_FC_NFT_TABLE", "user",
           "firecracker: the nftables isolation table. Must match the host's "
           "network pool.",
           "`bot_bottle_fc`; `bot_bottle_fc<k>` for slice k"),
    EnvVar("BOT_BOTTLE_FC_ORCH_IFACE", "user",
           "firecracker: the orchestrator VM's TAP. Must match the host's network pool.",
           "`bborch0`; `bborch<k>` for slice k"),
    EnvVar("BOT_BOTTLE_FC_GW_IFACE", "user",
           "firecracker: the gateway VM's TAP. Must match the host's network pool.",
           "`bbgw0`; `bbgw<k>` for slice k"),
    EnvVar("BOT_BOTTLE_FC_OWNER", "user",
           "firecracker network pool script: the user that owns the TAPs.",
           "`$SUDO_USER`, else `$USER`"),
    EnvVar("BOT_BOTTLE_FC_GROUP", "user",
           "firecracker network pool script: a group to own the TAPs instead of "
           "one user."),
    # --- user: the installer ---
    EnvVar("BOT_BOTTLE_PYTHON", "user",
           "Installer: the interpreter to install with; skips the search."),
    EnvVar("BOT_BOTTLE_VENV", "user",
           "Installer and `uninstall`: where the venv install lives when there's "
           "no `uv` or `pipx`.",
           "`~/.bot-bottle/venv`"),
    EnvVar("BOT_BOTTLE_INSTALL_SPEC", "user",
           "Installer: the pip spec to install instead of the default `git+` URL "
           "at the latest release tag, such as a wheel path or index name."),
    EnvVar("BOT_BOTTLE_INSTALL_REF", "user",
           "Installer: the git ref to install instead of the latest release tag, "
           "such as `v0.1.4`, or `main` (same as `--main`; then run `bot-bottle up "
           "--build`).",
           "the latest `vX.Y.Z` tag"),
    # --- dev ---
    EnvVar("BOT_BOTTLE_ORCHESTRATOR_IMAGE", "dev",
           "The orchestrator image to run.",
           "`bot-bottle-orchestrator:latest`"),
    EnvVar("BOT_BOTTLE_GATEWAY_IMAGE", "dev",
           "The gateway image to run.",
           "`bot-bottle-gateway:latest`"),
    EnvVar("BOT_BOTTLE_HOSTCTL_PORT", "dev",
           "The host controller's loopback port.",
           "ephemeral"),
    EnvVar("BOT_BOTTLE_DOCKER_CA_MOUNT", "dev",
           "docker in CI: a named volume for the gateway CA, for a runner that "
           "shares the host's Docker socket. " + GITHUB_CUTOVER),
    EnvVar("BOT_BOTTLE_DOCKER_ROOT_MOUNT", "dev",
           "docker in CI: a named volume for the state root the orchestrator "
           "mounts. " + GITHUB_CUTOVER),
    EnvVar("BOT_BOTTLE_DOCKER_CLIENT_NETWORK", "dev",
           "docker in CI: the job container's network, which the orchestrator "
           "joins. " + GITHUB_CUTOVER),
    # --- release ---
    EnvVar("BOT_BOTTLE_INFRA_ARTIFACT_BASE", "release",
           "The registry the images and rootfs artifacts are published to.",
           "`https://ghcr.io`"),
    EnvVar("BOT_BOTTLE_INFRA_ARTIFACT_OWNER", "release",
           "The registry owner the images are published under.",
           "`diderisoft`"),
    EnvVar("BOT_BOTTLE_INFRA_ARTIFACT_USER", "release",
           "The user the publish token belongs to (GHCR: the GitHub account).",
           "`bot-bottle`"),
    EnvVar("BOT_BOTTLE_INFRA_EXT4_STORE", "release",
           "Where the Firecracker rootfs is published: `oci` (an OCI artifact in "
           "the image registry) or `generic` (a Gitea generic package).",
           "`oci`"),
    EnvVar("BOT_BOTTLE_IMAGE_SOURCE_URL", "release",
           "The image's `org.opencontainers.image.source` label, which links the "
           "packages to the repository.",
           "`https://github.com/diderisoft/bot-bottle`"),
    # --- internal ---
    EnvVar("BOT_BOTTLE_ORCHESTRATOR_URL", "internal",
           "The orchestrator's control-plane URL, for the gateway's daemons."),
    EnvVar("BOT_BOTTLE_ORCHESTRATOR_TOKEN", "internal",
           "The `control-plane` signing key; the orchestrator refuses to start "
           "without it."),
    EnvVar("BOT_BOTTLE_ORCHESTRATOR_AUTH_JWT", "internal",
           "The gateway's `gateway`-role token for the control plane."),
    EnvVar("BOT_BOTTLE_HOSTCTL_URL", "internal",
           "The host controller's URL, for an orchestrator it brought up."),
    EnvVar("BOT_BOTTLE_HOSTCTL_KEY", "internal",
           "The `host-control` signing key."),
    EnvVar("BOT_BOTTLE_HOSTCTL_JWT", "internal",
           "A `host-control` token: `orchestrator`-role for the orchestrator, "
           "`gateway`-role for the gateway."),
    EnvVar("BOT_BOTTLE_GATEWAY_DAEMONS", "internal",
           "Which gateway daemons run (`egress`, `git-http`, `supervise`).",
           "all"),
    EnvVar("BOT_BOTTLE_GIT_GATE_READY_FILE", "internal",
           "When set, `git-http` waits for this file before it starts."),
    EnvVar("BOT_BOTTLE_SENSITIVE_PREFIXES", "internal",
           "Comma-separated env var name prefixes whose values egress DLP "
           "blocks as known secrets (the canary)."),
    EnvVar("BOT_BOTTLE_SOURCE_HASH", "internal",
           "macos-container: the source hash the running orchestrator was built "
           "from, to spot a stale one."),
    EnvVar("BOT_BOTTLE_CLAUDE_HOST_ACCESS_TOKEN", "internal",
           "Secret name the gateway injects a forwarded Claude login under."),
    EnvVar("BOT_BOTTLE_CODEX_HOST_ACCESS_TOKEN", "internal",
           "Secret name the gateway injects a forwarded Codex login under."),
    EnvVar("BOT_BOTTLE_VERSION", "internal",
           "The host bot-bottle version string, injected into infra containers at "
           "startup so `/version` can report it."),
    EnvVar("BOT_BOTTLE_COMMIT", "internal",
           "The host bot-bottle git commit hash, injected into infra containers at "
           "startup so `/version` can report it."),
    EnvVar("BOT_BOTTLE_PROTOCOL", "internal",
           "The host bot-bottle protocol version (integer), injected into infra "
           "containers at startup so `/version` can report it."),
)

# Names matched by a `BOT_BOTTLE_[A-Z0-9_]+` scan that aren't variables.
NOT_VARIABLES: dict[str, str] = {
    "BOT_BOTTLE_FC_": "prefix of the firecracker variables, in prose",
    "BOT_BOTTLE_DOCKER_": "prefix of the docker variables, in prose",
    "BOT_BOTTLE_BIN": "a local variable in install.sh, not read from the environment",
    "BOT_BOTTLE_IMAGE_": "prefix of the per-image version env vars; names are constructed dynamically",
}

BY_NAME: dict[str, EnvVar] = {v.name: v for v in ENV_VARS}


__all__ = ["AUDIENCES", "BY_NAME", "ENV_VARS", "EnvVar", "GITHUB_CUTOVER", "NOT_VARIABLES"]
