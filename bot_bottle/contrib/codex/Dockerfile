# bot-bottle Codex provider image.
#
# Mirrors the default Claude image shape: Node LTS, git/network tooling,
# non-root node user, and the provider CLI installed for that user.

FROM node:22.23.1-trixie-slim@sha256:e6d9a389d34ff9678438af985c9913fbd1eb6ed36e80fea56644f4b4f6dd70ba

# The standalone installer is used below because remote-control requires its
# managed package layout. Keep this exact release in sync with the verified
# installer source and checksum.
ARG CODEX_VERSION=0.145.0

ARG DEBIAN_SNAPSHOT=20260724T000000Z
RUN sed -i \
      -e "s|http://deb.debian.org/debian-security|https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|g" \
      -e "s|https://deb.debian.org/debian-security|https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|g" \
      -e "s|http://deb.debian.org/debian|https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|g" \
      -e "s|https://deb.debian.org/debian|https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|g" \
      /etc/apt/sources.list.d/debian.sources

RUN apt-get -o Acquire::Check-Valid-Until=false update \
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
RUN apt-get -o Acquire::Check-Valid-Until=false update \
  && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
  && rm -rf /var/lib/apt/lists/*

RUN install -d -o node -g node -m 755 /home/node/.config /home/node/.config/git

USER node
WORKDIR /home/node

ENV PATH="/home/node/.local/bin:${PATH}"

# Remote-control support requires the standalone Codex install layout under
# ~/.codex/packages/standalone/current. Fetch the installer from the same
# immutable release tag as the requested CLI, verify the committed checksum
# before executing it, and let the official installer verify the selected
# release archive against upstream release metadata.
COPY --chown=node:node bot_bottle/contrib/codex/install.sh.sha256 /tmp/install.sh.sha256
RUN curl -fsSL \
      "https://raw.githubusercontent.com/openai/codex/rust-v${CODEX_VERSION}/scripts/install/install.sh" \
      -o /tmp/install.sh \
  && cd /tmp \
  && sha256sum -c install.sh.sha256 \
  && CODEX_NON_INTERACTIVE=1 sh /tmp/install.sh --release "${CODEX_VERSION}" \
  && test "$(codex --version)" = "codex-cli ${CODEX_VERSION}"

CMD ["codex"]
