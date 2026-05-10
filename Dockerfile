# claude-bottle container image.
#
# Goal: a small, cache-friendly base that ships claude-code (the
# `@anthropic-ai/claude-code` npm package, CLI name `claude`) ready to run
# interactively. The container is ephemeral; per PRD 0001 v1 the host
# filesystem is not mounted in.
#
# Layer ordering is deliberate: the npm install lives in its own layer so
# changes to the rest of the repo (or to the CMD) don't bust it.

# Current Node LTS; slim variant keeps the image small while still
# providing apt-get for any future additions.
FROM node:22-slim

# Install runtime system deps. claude-code shells out to git for several
# features (status checks, commits, PR creation) — without git in the
# image, those features fail in surprising ways once the user does any
# real work. ca-certificates is already in the slim base; listed for
# clarity in case the base ever drops it. socat is the privileged
# forwarder for the in-container ssh-agent (see claude_bottle/ssh.py): the agent
# runs as root and rejects non-root connections, so socat sits between
# node and the agent socket.
RUN apt-get update \
  && apt-get install -y --no-install-recommends git ca-certificates openssh-client socat \
  && rm -rf /var/lib/apt/lists/*

# Install claude-code globally. Pinned to the version verified in the v1
# build (`claude --version` returns 2.1.126). Bump deliberately when
# rolling forward; an unpinned install would mean rebuilds silently pick
# up new behavior.
RUN npm install -g --no-fund --no-audit @anthropic-ai/claude-code@2.1.126 \
  && npm cache clean --force

# Run as a non-root user. The node image already provides a `node` user
# (uid 1000) with a home directory, which is where claude-code will write
# its session state.
USER node
WORKDIR /home/node

# Pre-create the skills directory so PRD 0002's host->container skill
# copier (claude_bottle/skills.py) drops files into a path owned by the
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
# `claude_bottle/cli/start.py` runs the container detached and uses `docker exec`
# to attach a TTY, but this CMD makes `docker run -it claude-bottle` also
# do something useful for ad-hoc debugging.
CMD ["claude"]
