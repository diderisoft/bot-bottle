# bot-bottle Codex provider image.
#
# Mirrors the default Claude image shape: Node LTS, git/network tooling,
# non-root node user, and the provider CLI installed globally.

FROM node:22-slim

RUN apt-get update \
  && apt-get install -y --no-install-recommends git ca-certificates curl \
  && rm -rf /var/lib/apt/lists/*

# App-specific deps. Python isn't required by codex itself
# (codex is a Node CLI), but is convenient for the agent to shell
# out to for ad-hoc scripts. Kept on its own layer so it can be
# moved to a downstream image if the base ever needs to shrink.
RUN apt-get update \
  && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
  && rm -rf /var/lib/apt/lists/*

RUN npm install -g --no-fund --no-audit @openai/codex@0.136.0 \
  && npm cache clean --force

USER node
WORKDIR /home/node

RUN mkdir -p /home/node/.codex

CMD ["codex"]
