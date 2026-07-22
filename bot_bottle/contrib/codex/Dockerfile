# bot-bottle Codex provider image.
#
# Mirrors the default Claude image shape: Node LTS, git/network tooling,
# non-root node user, and the provider CLI installed for that user.

FROM node:22-trixie-slim

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
  git \
  ca-certificates \
  curl \
  openssh-client \
  procps \
  ripgrep \
  && rm -rf /var/lib/apt/lists/*

# App-specific deps. Python isn't required by codex itself
# (codex is a Node CLI), but is convenient for the agent to shell
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
# under ~/.codex/packages/standalone/current. The npm package can run
# the TUI, but remote-control commands expect this installer-owned path.
RUN mkdir -p /home/node/.codex \
  && curl -fsSL https://chatgpt.com/codex/install.sh | sh

CMD ["codex"]
