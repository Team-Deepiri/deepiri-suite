# Deepiri Node.js base — published to ghcr.io/team-deepiri/deepiri-base
# Build from this repository root:
#   docker build --build-arg BASE_IMAGE=node:18-alpine -t ghcr.io/team-deepiri/deepiri-base:18-alpine .

ARG BASE_IMAGE=node:18-alpine
FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.source="https://github.com/Team-Deepiri/deepiri-base"
LABEL org.opencontainers.image.description="Deepiri Node runtime (dumb-init, bash, openssl on Alpine, K8s env scripts)"

WORKDIR /app

RUN set -eux; \
    if command -v apk >/dev/null 2>&1; then \
      apk add --no-cache curl dumb-init bash openssl; \
    else \
      apt-get update; \
      apt-get install -y --no-install-recommends curl ca-certificates openssl dumb-init bash; \
      rm -rf /var/lib/apt/lists/*; \
    fi

COPY scripts/load-k8s-env.sh /usr/local/bin/load-k8s-env.sh
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY scripts/prisma-baseline.sh /usr/local/bin/prisma-baseline.sh
RUN chmod +x /usr/local/bin/load-k8s-env.sh /usr/local/bin/docker-entrypoint.sh /usr/local/bin/prisma-baseline.sh

RUN set -eux; \
    if command -v apk >/dev/null 2>&1; then \
      addgroup -g 1001 -S nodejs && adduser -S nodejs -u 1001 -G nodejs; \
    else \
      groupadd -r nodejs -g 1001 && useradd -r -u 1001 -g nodejs -m -d /home/nodejs nodejs; \
    fi

RUN chown -R nodejs:nodejs /app
