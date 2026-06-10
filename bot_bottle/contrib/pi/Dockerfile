# bot-bottle Pi provider image.
#
# Node LTS, git/network tooling, and the Pi coding-agent CLI installed globally.

FROM node:22-slim

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
  git \
  ca-certificates \
  curl \
  fd-find \
  ripgrep \
  && ln -s /usr/bin/fdfind /usr/local/bin/fd \
  && rm -rf /var/lib/apt/lists/*

RUN apt-get update \
  && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
  && rm -rf /var/lib/apt/lists/*

RUN npm install -g --ignore-scripts --no-fund --no-audit @earendil-works/pi-coding-agent \
  && npm cache clean --force

RUN mkdir -p /home/node/.pi/agent \
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
