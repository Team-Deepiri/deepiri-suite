# deepiri-node-base

Shared Node.js Docker base for Team Deepiri services: `curl`, `dumb-init`, `bash`, OpenSSL (Alpine), K8s env loader scripts, and a non-root `nodejs` user (uid/gid 1001).

Images are published to **GitHub Container Registry**:

| Tag | Base | Typical services |
|-----|------|------------------|
| `ghcr.io/team-deepiri/deepiri-node-base:18-alpine` | `node:18-alpine` | api-gateway, external-bridge, challenge, engagement, notification, platform-analytics, realtime |
| `ghcr.io/team-deepiri/deepiri-node-base:18-slim` | `node:18-slim` | auth-service, task-orchestrator |
| `ghcr.io/team-deepiri/deepiri-node-base:20-alpine` | `node:20-alpine` | language-intelligence, messaging |

## First-time setup (GitHub)

1. Create an empty repository on GitHub: `Team-Deepiri/deepiri-node-base`.
2. Push this directory to `main` (see [REMOTE_SETUP.md](REMOTE_SETUP.md)).
3. Confirm the **Publish deepiri-node-base** workflow runs and all three tags appear under the org’s GitHub Packages.

**Important:** Publish this repository and verify images exist **before** merging service Dockerfiles that use `FROM ghcr.io/team-deepiri/deepiri-node-base:…`.

## Local build

From the repository root:

```bash
docker build --build-arg BASE_IMAGE=node:18-alpine -t ghcr.io/team-deepiri/deepiri-node-base:18-alpine .
```

## Pulling the image (private package)

If the GHCR package is private, authenticate once:

```bash
echo YOUR_GITHUB_TOKEN | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

In GitHub Actions, use `docker/login-action` with `registry: ghcr.io` and `GITHUB_TOKEN` (with `packages: read`) before `docker build` when the service Dockerfile uses this base.

## Scripts in the image

- `/usr/local/bin/load-k8s-env.sh` — loads env from mounted ConfigMap/Secret YAML under `/k8s-configmaps` and `/k8s-secrets`
- `/usr/local/bin/docker-entrypoint.sh` — sources the loader then `exec`s the container command
- `/usr/local/bin/prisma-baseline.sh` — optional Prisma baseline helper

Source of truth for script content is this repo; sync from `deepiri-platform` `platform-services/shared/scripts/` when those files change.

## Child Dockerfiles

Example:

```dockerfile
FROM ghcr.io/team-deepiri/deepiri-node-base:18-alpine
WORKDIR /app
COPY package*.json ./
# ... service-specific layers ...
USER nodejs
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["/usr/bin/dumb-init", "--", "node", "dist/server.js"]
```

The base image ends as **root** so you can run `npm ci` / builds as root, then `USER nodejs` when ready.
