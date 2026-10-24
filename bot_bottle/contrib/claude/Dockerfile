# Claude agent image: claude-code (`@anthropic-ai/claude-code`, CLI `claude`).
# The npm install is its own layer for caching.

# Version-qualified Node LTS, pinned to its multi-architecture manifest.
ARG NODE_BASE_IMAGE
FROM ${NODE_BASE_IMAGE}

ARG DEBIAN_SNAPSHOT=20260724T000000Z
RUN sed -i \
      -e "s|http://deb.debian.org/debian-security|http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|g" \
      -e "s|https://deb.debian.org/debian-security|http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|g" \
      -e "s|http://deb.debian.org/debian|http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|g" \
      -e "s|https://deb.debian.org/debian|http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|g" \
      /etc/apt/sources.list.d/debian.sources

# Runtime deps: claude-code shells out to git; curl works through the egress
# proxy.
RUN apt-get -o Acquire::Check-Valid-Until=false update \
  && apt-get install -y --no-install-recommends \
  git \
  ca-certificates \
  curl \
  openssh-client \
  ripgrep \
  iproute2 \
  dnsutils \
  && rm -rf /var/lib/apt/lists/*

# Python for the agent's ad-hoc scripts.
RUN apt-get -o Acquire::Check-Valid-Until=false update \
  && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
  && rm -rf /var/lib/apt/lists/*

# Install from the committed npm lock. `npm ci` verifies every registry
# artifact against its lockfile integrity and refuses dependency drift.
COPY bot_bottle/contrib/claude/package.json \
     bot_bottle/contrib/claude/package-lock.json /opt/claude/
RUN cd /opt/claude \
  && npm ci --omit=dev --no-fund --no-audit \
  && ln -s /opt/claude/node_modules/.bin/claude /usr/local/bin/claude \
  && npm cache clean --force

# Git reads both ~/.gitconfig and ~/.config/git/config. Keep its XDG config
# path traversable by the non-root runtime user so permission errors do not
# suppress bot-bottle's git-gate insteadOf rules.
RUN install -d -o node -g node -m 755 /home/node/.config /home/node/.config/git

# Run as the image's non-root `node` user (uid 1000).
USER node
WORKDIR /home/node

# Skills are copied here; owned by `node`.
RUN mkdir -p /home/node/.claude/skills

# Unquoted delimiter so $HOME expands (no other `$` in the body).
RUN cat > "$HOME/.claude.json" <<JSON
{
  "hasCompletedOnboarding": true,
  "theme": "dark",
  "bypassPermissionsModeAccepted": true,
  "projects": {
    "$HOME": { "hasTrustDialogAccepted": true }
  }
}
JSON

# Interactive claude, for `docker run -it` debugging.
CMD ["claude"]
