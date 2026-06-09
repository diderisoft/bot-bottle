# bot-bottle Pi provider image.
#
# Node LTS, git/network tooling, and the Pi coding-agent CLI installed globally.

FROM node:22-slim

RUN apt-get update \
  && apt-get install -y --no-install-recommends git ca-certificates curl \
  && rm -rf /var/lib/apt/lists/*

RUN apt-get update \
  && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
  && rm -rf /var/lib/apt/lists/*

RUN npm install -g --ignore-scripts --no-fund --no-audit @earendil-works/pi-coding-agent \
  && npm cache clean --force

USER node
WORKDIR /home/node

RUN mkdir -p /home/node/.pi/agent

CMD ["pi"]
