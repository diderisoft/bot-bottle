# bot-bottle Pi provider image.
#
# Node LTS, git/network tooling, and the Pi coding-agent CLI installed globally.

ARG NODE_BASE_IMAGE
FROM ${NODE_BASE_IMAGE}

ARG DEBIAN_SNAPSHOT=20260724T000000Z
RUN sed -i \
      -e "s|http://deb.debian.org/debian-security|http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|g" \
      -e "s|https://deb.debian.org/debian-security|http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|g" \
      -e "s|http://deb.debian.org/debian|http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|g" \
      -e "s|https://deb.debian.org/debian|http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|g" \
      /etc/apt/sources.list.d/debian.sources

# On macOS, bot-bottle loads the agent's firewall with nftables.
RUN apt-get -o Acquire::Check-Valid-Until=false update \
  && apt-get install -y --no-install-recommends \
  git \
  ca-certificates \
  curl \
  fd-find \
  nftables \
  openssh-client \
  ripgrep \
  && ln -s /usr/bin/fdfind /usr/local/bin/fd \
  && rm -rf /var/lib/apt/lists/*

RUN apt-get -o Acquire::Check-Valid-Until=false update \
  && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
  && rm -rf /var/lib/apt/lists/*

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

# Pi discovers npm packages from its agent directory. Installing the CLI and
# extensions together from the committed lock makes all direct and transitive
# package versions deterministic, with npm integrity verification.
COPY --chown=node:node bot_bottle/contrib/pi/package.json \
     bot_bottle/contrib/pi/package-lock.json /home/node/.pi/agent/
RUN cd /home/node/.pi/agent \
  && npm ci --omit=dev --no-fund --no-audit \
  && npm cache clean --force

ENV PATH="/home/node/.pi/agent/node_modules/.bin:${PATH}"

CMD ["pi"]
