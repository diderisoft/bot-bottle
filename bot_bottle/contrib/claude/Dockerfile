# bot-bottle container image.
#
# Goal: a small, cache-friendly base that ships claude-code (the
# `@anthropic-ai/claude-code` npm package, CLI name `claude`) ready to run
# interactively. The container is ephemeral; per PRD 0001 v1 the host
# filesystem is not mounted in.
#
# Layer ordering is deliberate: the npm install lives in its own layer so
# changes to the rest of the repo (or to the CMD) don't bust it.

# Version-qualified Node LTS, pinned to its multi-architecture manifest.
FROM node:22.23.1-trixie-slim@sha256:e6d9a389d34ff9678438af985c9913fbd1eb6ed36e80fea56644f4b4f6dd70ba

ARG DEBIAN_SNAPSHOT=20260724T000000Z
RUN sed -i \
      -e "s|http://deb.debian.org/debian-security|http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|g" \
      -e "s|https://deb.debian.org/debian-security|http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|g" \
      -e "s|http://deb.debian.org/debian|http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|g" \
      -e "s|https://deb.debian.org/debian|http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|g" \
      /etc/apt/sources.list.d/debian.sources

# Install runtime system deps. claude-code shells out to git for several
# features (status checks, commits, PR creation) — without git in the
# image, those features fail in surprising ways once the user does any
# real work. ca-certificates is already in the slim base; listed for
# clarity in case the base ever drops it. curl is here so any
# HTTPS_PROXY-aware tool (curl itself, plus anything that shells out
# to it) works against egress's bumped TLS without the agent needing
# local DNS.
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

# App-specific deps. Python isn't required by claude-code itself
# (claude-code is a Node CLI), but is convenient for the agent to
# shell out to for ad-hoc scripts. Kept on its own layer so it can
# be moved to a downstream image if the base ever needs to shrink.
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

# Run as a non-root user. The node image already provides a `node` user
# (uid 1000) with a home directory, which is where claude-code will write
# its session state.
USER node
WORKDIR /home/node

# Pre-create the skills directory so PRD 0002's host->container skill
# copier (bot_bottle/skills.py) drops files into a path owned by the
# `node` user. `skills_copy_into` also `mkdir -p`s defensively, but
# baking it into the image avoids a permission-confusion footgun if a
# future change to the launcher copies in as a different user.
RUN mkdir -p /home/node/.claude/skills

# Heredoc delimiter is unquoted so $HOME expands; no other `$` appears
# in the body, so this is safe under dash (Docker's default RUN shell).
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

# Default to an interactive claude session. In the v1 launcher,
# `bot_bottle/cli/start.py` runs the container detached and uses `docker exec`
# to attach a TTY, but this CMD makes `docker run -it bot-bottle-claude` also
# do something useful for ad-hoc debugging.
CMD ["claude"]
