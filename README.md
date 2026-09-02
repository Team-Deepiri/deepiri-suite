# deepiri-suite

Shared Deepiri toolchain repository. Today it publishes the Node.js Docker base for Team Deepiri services: `curl`, `dumb-init`, `bash`, OpenSSL (Alpine), K8s env loader scripts, and a non-root `nodejs` user (uid/gid 1001).

## PR QA Test Planner

`scripts/pr-qa-planner.py` is the one script for Team-Deepiri PR QA work: it
tells QA engineers what to test on any pull request, and separately checks
that reviews actually report what was tested.

No manual setup: on first run it auto-installs the `gh` CLI via your
platform's package manager if it's missing, then runs `gh auth login` if
you're not authenticated. Nothing else to install — stdlib only.

**Test planning** — including submodule-bump PRs against the
`deepiri-platform` (cloud) or `deepiri-control-plane` (local/lab) monorepo:

```bash
# Local analysis
python3 scripts/pr-qa-planner.py --repo Team-Deepiri/deepiri-platform --pr 316

# Post the test plan as a comment on the PR (QA checks off the checklist)
python3 scripts/pr-qa-planner.py --repo Team-Deepiri/deepiri-api-gateway --pr 42 --comment

# Scan a local checkout to find exactly which tests exercise the changed code
python3 scripts/pr-qa-planner.py --repo Team-Deepiri/deepiri-auth-service --pr 74 --deep

# Machine-readable output for CI / other tooling
python3 scripts/pr-qa-planner.py --repo Team-Deepiri/deepiri-platform --pr 316 --json
```

The plan follows the `deepiri-qa-workflow` skill: task identification
(Plaky/inbox/cross-PR deps/submodule-bump commits), environment bring-up via
the consolidated `setup-deepiri-dev.sh --team qa` script (in **deepiri-control-plane**) (adds `--build`
automatically when the PR touches lockfiles/Dockerfiles/submodule pointers),
health check + `/sorge` first pass, frontend/backend verification, and the
review-submission rule (Approve or Request Changes, never Comment-only).

**Review-quality enforcement** — checks that an Approve/Request-Changes
review actually filled in the required test-report template (Environment,
Health check, Sorge pass, Manual testing, Automated tests) instead of
rubber-stamping with a bare "LGTM" or leaving the bracketed placeholders:

```bash
# Check one PR's latest final review
python3 scripts/pr-qa-planner.py --review-check --repo Team-Deepiri/deepiri-auth-service --pr 74

# Same, and post a nudge comment tagging the reviewer if it's incomplete
python3 scripts/pr-qa-planner.py --review-check --repo Team-Deepiri/deepiri-auth-service --pr 74 --nudge

# Compliance report across a repo's recent merged PRs, by reviewer
python3 scripts/pr-qa-planner.py --review-check --sweep --repo Team-Deepiri/deepiri-auth-service --limit 30

# Same, across every repo the QA team reviews
python3 scripts/pr-qa-planner.py --review-check --sweep --all-repos --limit 30
```

**Interactive terminal report** — run with `--interactive` (or with no
arguments on a TTY) to open the plan in a scrollable terminal UI instead of
static markdown: `↑/↓` or `j/k` move, `space` toggles checklist items,
`e` exports the current state to a file, `c` copies the whole report,
`l` copies just the cursor line's command, and `p` builds an AI-prompt
bundle (plan + deep-scan context). Copying uses OSC 52 — the terminal
itself sets the clipboard — so it works over SSH and inside tmux without
xclip/wl-copy/pbcopy installed.

**Guided walkthrough** — press `w` in the interactive report to step
through every actionable line one at a time, navigated with `←`/`→`: run a
step's command inline with `r` (output shown in the same terminal), open
visual-check URLs with `o`, tick checkboxes with `space`. Every step of the
plan is included — health checks, per-area test commands, per-route curls,
and the individual backend functions this PR changed.

See the module docstring for the full option list.

Images are published to **GitHub Container Registry**:

| Tag | Base | Typical services |
|-----|------|------------------|
| `ghcr.io/team-deepiri/deepiri-suite:18-alpine` | `node:18-alpine` | api-gateway, external-bridge, challenge, engagement, notification, platform-analytics, realtime |
| `ghcr.io/team-deepiri/deepiri-suite:18-slim` | `node:18-slim` | auth-service, task-orchestrator |
| `ghcr.io/team-deepiri/deepiri-suite:20-alpine` | `node:20-alpine` | language-intelligence, messaging |

## First-time setup (GitHub)

1. Create an empty repository on GitHub: `Team-Deepiri/deepiri-suite`.
2. Push this directory to `main` (see [REMOTE_SETUP.md](REMOTE_SETUP.md)).
3. Confirm the **Publish deepiri-suite** workflow runs and all three tags appear under the org’s GitHub Packages.

**Important:** Publish this repository and verify images exist **before** merging service Dockerfiles that use `FROM ghcr.io/team-deepiri/deepiri-suite:…`.

## Local build

From the repository root:

```bash
docker build --build-arg BASE_IMAGE=node:18-alpine -t ghcr.io/team-deepiri/deepiri-suite:18-alpine .
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

Source of truth for script content is this repo; sync from `deepiri-control-plane` (or platform) `platform-services/shared/scripts/` when those files change.

## Child Dockerfiles

Example:

```dockerfile
FROM ghcr.io/team-deepiri/deepiri-suite:18-alpine
WORKDIR /app
COPY package*.json ./
# ... service-specific layers ...
USER nodejs
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["/usr/bin/dumb-init", "--", "node", "dist/server.js"]
```

The base image ends as **root** so you can run `npm ci` / builds as root, then `USER nodejs` when ready.
