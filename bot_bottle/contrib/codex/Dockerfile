# Codex agent image: Node LTS, git/network tooling, non-root `node`, the codex
# CLI.

ARG NODE_BASE_IMAGE
FROM ${NODE_BASE_IMAGE}

# Remote-control requires the standalone package layout. Keep this exact release
# in sync with the committed upstream archive checksums.
ARG CODEX_VERSION=0.145.0

ARG DEBIAN_SNAPSHOT=20260724T000000Z
RUN sed -i \
      -e "s|http://deb.debian.org/debian-security|http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|g" \
      -e "s|https://deb.debian.org/debian-security|http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|g" \
      -e "s|http://deb.debian.org/debian|http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|g" \
      -e "s|https://deb.debian.org/debian|http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|g" \
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

# Python for the agent's ad-hoc scripts.
RUN apt-get -o Acquire::Check-Valid-Until=false update \
  && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
  && rm -rf /var/lib/apt/lists/*

RUN install -d -o node -g node -m 755 /home/node/.config /home/node/.config/git

USER node
WORKDIR /home/node

ENV PATH="/home/node/.local/bin:${PATH}"

# The standalone release for the target arch, checked against the committed
# upstream SHA256SUMS.
COPY --chown=node:node bot_bottle/contrib/codex/codex-package_SHA256SUMS /tmp/codex-package_SHA256SUMS
RUN case "$(dpkg --print-architecture)" in \
      amd64) codex_target=x86_64-unknown-linux-musl ;; \
      arm64) codex_target=aarch64-unknown-linux-musl ;; \
      *) echo "unsupported Codex architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac \
  && codex_asset="codex-package-${codex_target}.tar.gz" \
  && codex_sha256="$(awk -v asset="${codex_asset}" '$2 == asset { print $1 }' /tmp/codex-package_SHA256SUMS)" \
  && test -n "${codex_sha256}" \
  && curl -fsSL \
      "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/${codex_asset}" \
      -o "/tmp/${codex_asset}" \
  && echo "${codex_sha256}  /tmp/${codex_asset}" | sha256sum -c - \
  && codex_release="/home/node/.codex/packages/standalone/releases/${CODEX_VERSION}-${codex_target}" \
  && mkdir -p "${codex_release}" /home/node/.local/bin \
  && tar -xzf "/tmp/${codex_asset}" -C "${codex_release}" \
  && ln -s bin/codex "${codex_release}/codex" \
  && ln -s "${codex_release}" /home/node/.codex/packages/standalone/current \
  && ln -s /home/node/.codex/packages/standalone/current/bin/codex /home/node/.local/bin/codex \
  && rm "/tmp/${codex_asset}" \
  && test "$(codex --version)" = "codex-cli ${CODEX_VERSION}"

CMD ["codex"]
