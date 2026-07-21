# bot-bottle Pi provider image.
#
# Debian stable, git/network tooling, and the Pi coding-agent CLI.

FROM debian:trixie-slim

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
  git \
  ca-certificates \
  curl \
  fd-find \
  openssh-client \
  podman \
  ripgrep \
  nodejs \
  npm \
  && ln -s /usr/bin/fdfind /usr/local/bin/fd \
  && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 node \
  && useradd --uid 1000 --gid node --shell /bin/bash --create-home node

RUN apt-get update \
  && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
  && rm -rf /var/lib/apt/lists/*

RUN npm install -g --ignore-scripts --no-fund --no-audit @earendil-works/pi-coding-agent \
  && npm cache clean --force

RUN install -d -o node -g node -m 755 /home/node/.config /home/node/.config/git \
  && mkdir -p /home/node/.pi/agent \
  /home/node/.pi/context-mode/sessions \
  /tmp/pi-subagents-uid-1000 \
  && chown -R node:node /home/node/.pi /tmp \
  && chmod -R u+rwX /tmp \
  && chown root:root /tmp /var/tmp \
  && chmod 1777 /tmp /var/tmp

USER node
WORKDIR /home/node

RUN pi install npm:@harms-haus/pi-cwd \
  && pi install npm:pi-web-access \
  && pi install npm:context-mode \
  && pi install npm:pi-subagents \
  && pi install npm:pi-mcp-adapter

CMD ["pi"]
