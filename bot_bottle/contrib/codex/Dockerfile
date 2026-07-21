# bot-bottle Codex provider image.
#
# Mirrors the default Claude image shape: Debian stable, git/network tooling,
# a non-root runtime user, and the provider CLI installed for that user.

FROM debian:trixie-slim

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
  git \
  ca-certificates \
  curl \
  openssh-client \
  podman \
  procps \
  ripgrep \
  && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 node \
  && useradd --uid 1000 --gid node --shell /bin/bash --create-home node

# App-specific deps. Python isn't required by Codex itself, but is convenient
# for the agent to shell
# out to for ad-hoc scripts. Kept on its own layer so it can be
# moved to a downstream image if the base ever needs to shrink.
RUN apt-get update \
  && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
  && rm -rf /var/lib/apt/lists/*

RUN install -d -o node -g node -m 755 /home/node/.config /home/node/.config/git

USER node
WORKDIR /home/node

ENV PATH="/home/node/.local/bin:${PATH}"

# Remote-control support requires the standalone Codex install layout
# under ~/.codex/packages/standalone/current. Remote-control commands expect
# this installer-owned path.
RUN mkdir -p /home/node/.codex \
  && curl -fsSL https://chatgpt.com/codex/install.sh | sh

CMD ["codex"]
