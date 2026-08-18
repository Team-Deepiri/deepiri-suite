#!/usr/bin/env python3
"""
PR QA Test Planner for Team-Deepiri.

Automatically tells QA engineers what to test on any pull request. Given a
repo + PR, it:

  1. Fetches the PR metadata and changed-file list via the GitHub API (gh).
  2. Maps every changed path to an affected service/area (including submodule
     bumps in the deepiri-platform monorepo).
  3. Flags risk signals: Dockerfile changes, lockfile/dependency churn,
     DB migrations, CI workflow edits, secrets/env changes, and changes that
     ship without any accompanying test.
  4. Emits a markdown test plan: risk flags, per-area test commands, and a
     manual QA checklist the engineer can check off.

Run it against any PR in the org:

    python3 scripts/pr-qa-planner.py --repo Team-Deepiri/deepiri-platform --pr 316
    python3 scripts/pr-qa-planner.py --repo Team-Deepiri/deepiri-api-gateway --pr 42

Options:
    --repo OWNER/REPO   Repository to analyze (default: current gh repo).
    --pr NUMBER         PR number (default: PR for the current branch, if any).
    --json              Print the machine-readable plan as JSON.
    --comment           Post the test plan as a comment on the PR.
    --out FILE          Also write the markdown plan to FILE.
    --org NAME          GitHub org owning the repos (default: Team-Deepiri).

Requires the `gh` CLI authenticated for the org (see
https://cli.github.com). No other dependencies.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

ORG = "Team-Deepiri"

# ---------------------------------------------------------------------------
# Service map: path prefix -> area. In the platform monorepo a submodule bump
# (a one-line gitlink diff) means "this sub-service changed"; a path under a
# service's tree means the change lives inside that service repo directly.
# ---------------------------------------------------------------------------

# Prefix -> (area label, test_command). Command is a suggestion; QA should
# adapt to the actual repo's setup.
SERVICE_MAP = [
    # -- platform monorepo: backend services
    ("platform-services/backend/deepiri-api-gateway", "API Gateway", "cd platform-services/backend/deepiri-api-gateway && npm test"),
    ("platform-services/backend/deepiri-auth-service", "Auth Service", "cd platform-services/backend/deepiri-auth-service && npm test"),
    ("platform-services/backend/deepiri-language-intelligence-service", "Language Intelligence", "cd platform-services/backend/deepiri-language-intelligence-service && npm test"),
    ("platform-services/backend/deepiri-external-bridge-service", "External Bridge", "cd platform-services/backend/deepiri-external-bridge-service && npm test"),
    ("platform-services/backend/deepiri-messaging-service", "Messaging Service", "cd platform-services/backend/deepiri-messaging-service && npm test"),
    ("platform-services/backend/deepiri-realtime-gateway", "Realtime Gateway", "cd platform-services/backend/deepiri-realtime-gateway && npm test"),
    ("platform-services/backend/deepiri-telemetry", "Telemetry", "cd platform-services/backend/deepiri-telemetry && npm test"),
    ("platform-services/backend/deepiri-registry", "Registry", "cd platform-services/backend/deepiri-registry && npm test"),
    ("platform-services/backend/deepiri-jobs", "Jobs", "cd platform-services/backend/deepiri-jobs && npm test"),
    ("platform-services/backend/deepiri-truss", "Truss", "cd platform-services/backend/deepiri-truss && npm test"),
    ("platform-services/backend/deepiri-speech", "Speech", "cd platform-services/backend/deepiri-speech && npm test"),
    ("platform-services/backend/deepiri-workflow-orchestrator", "Workflow Orchestrator", "cd platform-services/backend/deepiri-workflow-orchestrator && npm test"),
    ("platform-services/backend/deepiri-communications-hub", "Communications Hub", "cd platform-services/backend/deepiri-communications-hub && npm test"),
    ("platform-services/backend/deepiri-decision-intelligence", "Decision Intelligence", "cd platform-services/backend/deepiri-decision-intelligence && npm test"),
    ("platform-services/backend/deepiri-adaptive-experience-engine", "Adaptive Experience", "cd platform-services/backend/deepiri-adaptive-experience-engine && npm test"),
    ("platform-services/backend/deepiri-incentive-engine", "Incentive Engine", "cd platform-services/backend/deepiri-incentive-engine && npm test"),
    # -- platform monorepo: shared libraries
    ("platform-services/shared/deepiri-prismpipe", "PrismPipe (shared)", "cd platform-services/shared/deepiri-prismpipe && pytest"),
    ("platform-services/shared/deepiri-synapse", "Synapse (shared)", "cd platform-services/shared/deepiri-synapse && pytest"),
    ("platform-services/shared/deepiri-sugar-glider", "Sugar Glider (shared)", "cd platform-services/shared/deepiri-sugar-glider && pytest"),
    ("platform-services/shared/deepiri-shared-utils", "Shared Utils", "cd platform-services/shared/deepiri-shared-utils && npm test"),
    # -- platform monorepo: top-level submodule repos
    ("deepiri-web-frontend", "Web Frontend", "cd deepiri-web-frontend && npm test"),
    ("deepiri-modelkit", "Modelkit", "cd deepiri-modelkit && pytest"),
    ("deepiri-logger", "Logger", "cd deepiri-logger && pytest"),
    ("deepiri-ollama-utils", "Ollama Utils", "cd deepiri-ollama-utils && pytest"),
    ("deepiri-core-api", "Core API", "cd deepiri-core-api && pytest"),
    ("diri-cyrex", "Cyrex", "cd diri-cyrex && pytest"),
    ("diri-helox", "Helox", "cd diri-helox && pytest"),
    ("diri-persola", "Persola", "cd diri-persola && pytest"),
    ("deepiri-suite", "Deepiri Suite (toolchain)", "cd deepiri-suite && docker build ."),
    # -- platform monorepo: infrastructure and tooling
    ("deploy/", "Deployment/K8s", "helm lint + kubectl apply --dry-run=client"),
    ("ops/", "Ops", "cd ops && make lint"),
    ("skaffold/", "Skaffold", "skaffold render --validate"),
    ("teams/", "Team Dev Environments", "manual smoke of affected team env"),
    ("scripts/", "Repo Scripts", "python3 -m py_compile <changed scripts>"),
    (".github/workflows/", "CI Workflows", "PR CI run"),
]

# Risk-signal keyword -> (flag, QA guidance)
RISK_RULES = [
    ("dockerfile", "Dockerfile change", "Verify the image builds and the affected service boots. Check entrypoint/env-loader behavior if under deepiri-suite."),
    ("docker-compose", "Compose change", "Verify `docker compose config` is valid and affected services start together."),
    ("package-lock.json", "JS dependency churn", "Confirm `npm ci`/`yarn install` succeeds and the affected service still boots. Smoke-test features that pull in the changed dependency."),
    ("yarn.lock", "JS dependency churn", "Confirm `yarn install --frozen-lockfile` succeeds and the affected service still boots."),
    ("poetry.lock", "Python dependency churn", "Confirm `poetry install --lock` resolves and the affected service imports cleanly."),
    ("requirements.txt", "Python dependency churn", "Confirm the pip install path works; run the affected service's pytest suite."),
    ("migration", "DB migration", "Run the migration against a fresh DB and a copy of current data. Verify rollback/down migration if present."),
    ("schema", "DB schema", "Confirm schema change is backward compatible with running services; test affected endpoints."),
    (".env", "Environment/config change", "Confirm no secrets were committed. Verify the new env var is documented and the service reads it."),
    ("secret", "Secrets touch", "CRITICAL: confirm no secrets/credentials are in the diff. Block merge if any are."),
    ("graphql", "GraphQL change", "Test the changed resolvers/queries for null-safety, auth, and permissions."),
    ("openapi", "OpenAPI/spec change", "Verify the generated clients/schemas still match the new spec."),
    ("protobuf", "Protobuf change", "Regenerate bindings and verify wire compatibility between producers/consumers."),
]

# Extensions that count as "a test was included" when they appear alongside code.
TEST_MARKERS = [
    ".test.", "_test.py", "test_", "/tests/", "/test/", ".spec.",
    "integration_test", "e2e", "e2e.", "playwright", "cypress",
]

# Extensions that are never code (docs/CI/asset-only).
NON_CODE = {".md", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
            ".lock", ".gitignore", ".editorconfig", ".prettierrc", ".dockerignore"}


# ---------------------------------------------------------------------------
# gh plumbing
# ---------------------------------------------------------------------------

def gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh"] + list(args), capture_output=True, text=True)


def gh_json(path: str) -> dict:
    result = gh("api", path, "--jq", ".")
    if result.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_path(path: str) -> tuple[str, str]:
    """Return (area_label, test_command) for a path, or (None, None) if unknown."""
    low = path.lower()
    for prefix, label, cmd in SERVICE_MAP:
        if low.startswith(prefix):
            return label, cmd
    return None, None


def includes_test(file_paths: list[str]) -> bool:
    return any(any(m in f for m in TEST_MARKERS) for f in file_paths)


def non_code_only(file_paths: list[str]) -> bool:
    return bool(file_paths) and all(
        f.lower().endswith(tuple(NON_CODE)) for f in file_paths
    )


# Files whose presence means a plain `start.sh` is not enough — QA must run
# `build.sh` first (stale images silently produce false failures otherwise).
REBUILD_TRIGGERS = [
    "dockerfile", "docker-compose", "compose.yml", "compose.yaml",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "requirements", "go.mod", "go.sum", "alpine",
]


def is_gitlink_change(f: dict) -> bool:
    """True when the file is a submodule pointer bump, not a regular file edit."""
    return f.get("raw_url") is None and "Subproject commit" in (f.get("patch") or "")


SUBPROJECT_SHA_RE = re.compile(r"^\+Subproject commit ([0-9a-f]{40})", re.MULTILINE)

# Cross-repo refs like "Team-Deepiri/deepiri-web-frontend#123" — unambiguous,
# no keyword needed.
CROSS_REPO_REF_RE = re.compile(r"([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)#(\d+)")
# Same-repo refs only count as a dependency link when introduced by a keyword
# (otherwise "#123" in prose gives false positives).
KEYWORD_REF_RE = re.compile(
    r"(?im)^\s*(?:depends on|blocked by|related to|relates to|see also|requires)\s*:?\s*#(\d+)"
)


def fetch_pr_summary(repo: str, number: int) -> dict | None:
    r = gh("api", f"repos/{repo}/pulls/{number}",
           "--jq", "{number:.number,title:.title,url:.html_url,state:.state}")
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def find_referenced_prs(repo: str, pr_body: str, exclude_number: int = 0) -> list[dict]:
    """PRs explicitly referenced in the PR description — cross-repo links or
    same-repo refs introduced by a dependency keyword (depends on / blocked by
    / relates to / see also / requires)."""
    if not pr_body:
        return []
    found: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for ref_repo, num_s in CROSS_REPO_REF_RE.findall(pr_body):
        num = int(num_s)
        key = (ref_repo.lower(), num)
        if key in seen or (ref_repo.lower() == repo.lower() and num == exclude_number):
            continue
        seen.add(key)
        summary = fetch_pr_summary(ref_repo, num)
        if summary:
            summary["repo"] = ref_repo
            found.append(summary)
    for num_s in KEYWORD_REF_RE.findall(pr_body):
        num = int(num_s)
        key = (repo.lower(), num)
        if key in seen or num == exclude_number:
            continue
        seen.add(key)
        summary = fetch_pr_summary(repo, num)
        if summary:
            summary["repo"] = repo
            found.append(summary)
    return found


def resolve_submodule_bumps(files: list[dict], org: str) -> list[dict]:
    """For each submodule pointer bump, find the exact target commit and the
    PR in that submodule's repo (if any) that introduced it — so QA knows
    precisely which branch/commit to check the submodule out to."""
    out = []
    for f in files:
        if not is_gitlink_change(f):
            continue
        path = f["filename"]
        short = path.rstrip("/").split("/")[-1]
        sub_repo = f"{org}/{short}"
        m = SUBPROJECT_SHA_RE.search(f.get("patch") or "")
        sha = m.group(1) if m else None
        pr_info = None
        if sha:
            r = gh("api", f"repos/{sub_repo}/commits/{sha}/pulls",
                   "--jq", "(.[0] // empty) | {number,title,url:.html_url,head_ref:.head.ref}")
            if r.returncode == 0 and r.stdout.strip():
                try:
                    pr_info = json.loads(r.stdout)
                except json.JSONDecodeError:
                    pr_info = None
        out.append({
            "path": path, "repo": sub_repo, "sha": sha,
            "pr": pr_info or None,
        })
    return out


def find_related_prs(repo: str, head_ref: str, exclude_number: int = 0) -> list[dict]:
    """Other open PRs sharing this head branch — a cross-PR dependency QA should
    test together (shared branch, dependent submodule bump)."""
    if not head_ref:
        return []
    result = gh("pr", "list", "--repo", repo, "--state", "open",
                "--head", head_ref, "--json", "number,title,url", "--limit", "20")
    if result.returncode != 0:
        return []
    try:
        return [p for p in json.loads(result.stdout)
                if p.get("number") != exclude_number]
    except json.JSONDecodeError:
        return []


def analyze(files: list[dict], changed_files_before: int = 0) -> dict:
    """Build the plan model from the PR's changed files."""
    areas: dict[str, dict] = defaultdict(
        lambda: {"files": [], "additions": 0, "deletions": 0}
    )
    risks: list[dict] = []
    all_paths = [f["filename"] for f in files]
    low_paths = [p.lower() for p in all_paths]
    submodule_bumps = [f["filename"] for f in files if is_gitlink_change(f)]
    rebuild_needed = any(
        any(t in p for t in REBUILD_TRIGGERS) for p in low_paths
    ) or bool(submodule_bumps)

    for f in files:
        label, cmd = classify_path(f["filename"])
        area = areas[label] if label else areas["(unknown / other)"]
        area["files"].append(f["filename"])
        area["additions"] += f.get("additions", 0)
        area["deletions"] += f.get("deletions", 0)
        if label and not area.get("test_command"):
            area["test_command"] = cmd

    for keyword, flag, guidance in RISK_RULES:
        if any(keyword in p for p in low_paths):
            risks.append({"flag": flag, "guidance": guidance, "keyword": keyword})

    # Dedupe: package-lock.json + yarn.lock in the same PR shouldn't emit two
    # identical "JS dependency churn" rows.
    seen_flags: set[str] = set()
    unique_risks = []
    for r in risks:
        if r["flag"] not in seen_flags:
            seen_flags.add(r["flag"])
            unique_risks.append(r)
    risks = unique_risks

    code_files = [p for p in all_paths if not p.lower().endswith(tuple(NON_CODE))]
    tests_included = includes_test(all_paths)
    if code_files and not tests_included and not non_code_only(all_paths):
        risks.append({
            "flag": "No tests included",
            "guidance": ("This PR changes code but adds no test file. QA should "
                         "expand manual coverage and flag whether automated tests "
                         "are expected for the change."),
            "keyword": "no-tests",
        })

    for area in areas.values():
        area["files"] = sorted(area["files"])

    return {
        "areas": dict(areas),
        "risks": risks,
        "total_files": len(all_paths),
        "total_additions": sum(f.get("additions", 0) for f in files),
        "total_deletions": sum(f.get("deletions", 0) for f in files),
        "tests_included": tests_included,
        "unknown_files": areas.get("(unknown / other)", {}).get("files", []),
        "submodule_bumps": submodule_bumps,
        "rebuild_needed": rebuild_needed,
        "frontend_touched": any("web-frontend" in p or "frontend" in p for p in low_paths),
        "backend_touched": any("platform-services/backend" in p or "backend" in p for p in low_paths),
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def render_markdown(plan: dict, pr: dict, repo: str) -> str:
    title = pr.get("title", "(untitled)")
    lines = []
    lines.append(f"## QA Test Plan — #{pr.get('number')}: {title}")
    lines.append("")
    lines.append(f"_Automated by `deepiri-suite/scripts/pr-qa-planner.py` · repo `{repo}` · "
                 "follows the `deepiri-qa-workflow` skill_")
    lines.append("")
    lines.append(f"- **Changed files:** {plan['total_files']} "
                 f"(+{plan['total_additions']}/-{plan['total_deletions']})")
    lines.append(f"- **Tests included in PR:** "
                 f"{'yes' if plan['tests_included'] else 'no'}")
    lines.append(f"- **Branch:** `{pr.get('head', {}).get('ref')}` → "
                 f"`{pr.get('base', {}).get('ref')}`")
    lines.append("")

    # Phase 1 — Task identification
    lines.append("### 1. Task identification")
    lines.append("")
    lines.append("- [ ] Confirm the assignment on the **Plaky board** and in the "
                 "**GitHub review inbox** (`github.com/pulls/inbox`)")
    related = find_related_prs(repo, pr.get("head", {}).get("ref", ""),
                               pr.get("number", 0))
    if related:
        lines.append("- **Cross-PR dependency detected** — same head branch has other "
                     "open PRs; test them together (shared branch / dependent "
                     "submodule bump):")
        for rp in related:
            lines.append(f"  - [#{rp['number']} {rp['title']}]({rp['url']})")
    else:
        lines.append("- No other open PR shares this head branch (tested in isolation)")

    referenced = plan.get("referenced_prs") or []
    if referenced:
        lines.append("- **Referenced PRs** (linked in the description — check these are "
                     "merged/tested together where relevant):")
        for rp in referenced:
            lines.append(f"  - [{rp['repo']}#{rp['number']} {rp['title']}]({rp['url']}) — {rp['state']}")

    bumps = plan.get("submodule_bump_details") or []
    if bumps:
        lines.append("- **Submodule bumps — check out these exact commits before testing:**")
        for b in bumps:
            sha_short = (b["sha"] or "?")[:8]
            if b["pr"]:
                p = b["pr"]
                lines.append(
                    f"  - `{b['path']}` → `{sha_short}` "
                    f"(via [{b['repo']}#{p['number']} {p['title']}]({p['url']}), "
                    f"branch `{p.get('head_ref', '?')}`) — "
                    f"`git -C {b['path']} checkout {b['sha']}`"
                )
            elif b["sha"]:
                lines.append(
                    f"  - `{b['path']}` → `{sha_short}` (no open PR found for this commit "
                    f"in `{b['repo']}` — likely already merged to its default branch) — "
                    f"`git -C {b['path']} checkout {b['sha']}`"
                )
            else:
                lines.append(f"  - `{b['path']}` — bump detected but target commit could not "
                             f"be parsed from the diff; check manually")
    lines.append("")

    # Phase 2 — Local environment setup
    lines.append("### 2. Local environment setup")
    lines.append("")
    lines.append("- Checkout the PR's head branch (not `main`) in each affected "
                 "submodule before starting the stack.")
    lines.append(f"- Environment (consolidated, replaces team_dev_environments): "
                 f"`bash setup-deepiri-dev.sh --team qa "
                 f"{'--build ' if plan['rebuild_needed'] else ''}[--tier 1|2|3]` — "
                 f"**{('rebuild required (lockfile/Dockerfile/submodule changes - add `--build` so stale images are not reused)') if plan['rebuild_needed'] else 'no rebuild needed, omit `--build`'}**")
    lines.append("- Tear down when done: `docker compose -f docker-compose.dev.yml down` "
                 "— don't leave stacks running between PRs.")
    lines.append("")

    # Phase 3 — Verification and testing
    lines.append("### 3. Verification and testing")
    lines.append("")
    lines.append("- [ ] **Health check first:** `docker compose -f docker-compose.dev.yml ps` "
                 "— confirm every container reports `healthy` before testing; a container "
                 "still initializing produces false PR failures.")
    lines.append("- [ ] **Sorge bot pass:** comment `/sorge` on the PR; treat it as a "
                 "first pass that informs (not replaces) manual review.")
    if plan["frontend_touched"]:
        lines.append("- [ ] **Frontend:** verify UI/UX against the design spec, not "
                     "just that the page loads.")
    if plan["backend_touched"]:
        lines.append("- [ ] **Backend:** verify functional requirements and data "
                     "integrity — what the change actually persists or returns, not "
                     "just that the endpoint responds.")
    lines.append("")
    lines.append("#### Areas affected")
    lines.append("")
    lines.append("| Area | Test command | Files | +/- |")
    lines.append("|------|--------------|-------|-----|")
    for label, area in sorted(plan["areas"].items()):
        cmd = area.get("test_command") or "manual"
        cmd = cmd.replace("|", "\\|")
        files_preview = ", ".join(f.split("/")[-1] for f in area["files"][:3])
        if len(area["files"]) > 3:
            files_preview += f", … +{len(area['files']) - 3}"
        lines.append(
            f"| {label} | `{cmd}` | {files_preview} | "
            f"+{area['additions']}/-{area['deletions']} |"
        )
    lines.append("")

    if plan["risks"]:
        lines.append("#### Risk signals")
        lines.append("")
        lines.append("| Risk | What to check |")
        lines.append("|------|---------------|")
        for r in plan["risks"]:
            lines.append(f"| **{r['flag']}** | {r['guidance']} |")
        lines.append("")

    if plan.get("deep"):
        lines.extend(render_deep(plan["deep"]))

    lines.append("#### Manual QA checklist")
    lines.append("")
    lines.append("- [ ] Run the per-area test commands above")
    lines.append("- [ ] Smoke-test the primary user flows touched by this change")
    lines.append("- [ ] Check for regressions in adjacent services (shared-lib changes ripple)")
    lines.append("- [ ] Confirm no secrets/credentials were introduced")
    lines.append("- [ ] Verify the PR matches the ticket/acceptance criteria")
    for r in plan["risks"]:
        if r["keyword"] != "no-tests":
            lines.append(f"- [ ] {r['flag']}: {r['guidance']}")
    lines.append("")

    # Phase 4 — Documentation requirement
    lines.append("### 4. Documentation")
    lines.append("")
    lines.append("Do **not** hold up this PR over missing README or changelog updates "
                 "— current guidance says those are not a review blocker.")
    lines.append("")

    # Phase 5 — Submitting the review
    lines.append("### 5. Submitting the review")
    lines.append("")
    lines.append("On the PR's **Files changed** tab, use **Submit review**. The summary "
                 "must say what you actually tested:")
    lines.append("")
    lines.append("```text")
    lines.append("Environment: [qa-team stack, build vs start]")
    lines.append("Health check: [all containers healthy / issues]")
    lines.append("Sorge pass: [what it flagged, how you handled it]")
    lines.append("Manual testing: [what you exercised, frontend/backend]")
    lines.append("Automated tests: [run / not run, why]")
    lines.append("```")
    lines.append("")
    lines.append("- Select **Approve** if good to go, **Request Changes** if not "
                 "(say specifically what needs looking into).")
    lines.append("- **Never leave a plain Comment-only review** — always resolve to "
                 "Approve or Request Changes.")
    lines.append("")

    lines.append("<details><summary>Changed files</summary>")
    lines.append("")
    for f in sorted(plan["areas"].get("(unknown / other)", {}).get("files", [])):
        lines.append(f"- `{f}`")
    lines.append("</details>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Deep analysis: what specifically to test, via local code scan
# ---------------------------------------------------------------------------
#
# Strategy to avoid a full recursive scan:
#   1. Only parse the *changed* files (small set) for symbols/routes.
#   2. Discover references with `rg` (ripgrep) — a single pass, gitignored
#      dirs skipped automatically, returns file lists, not AST walks.
#   3. Follow each changed file's imports to find its direct neighbors and
#      any test files that exercise it. Depth is bounded to changed files +
#      one import hop + one rg hop (who references the symbols).

PY_SYMBOL = re.compile(
    r"^(?:async\s+def|def)\s+(\w+)"
    r"|^class\s+(\w+)"
    r"|@\w+\.(?:get|post|put|delete|patch)\([\"']([^\"']*)",
    re.MULTILINE,
)
TS_SYMBOL = re.compile(
    r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)"
    r"|^(?:export\s+)?const\s+(\w+)\s*="
    r"|^export\s+default\s+(\w+)"
    r"|\.(?:get|post|put|delete|patch)\([\"']([^\"']*)"
    r"|^(?:export\s+)?class\s+(\w+)",
    re.MULTILINE,
)
IMPORT_RE = re.compile(r"(?:from\s+|require\(|import\s*\(?)[\"']([^\"']+)[\"']")


# Symbols too generic to be worth a reverse-reference hop (they'd match half
# the repo); still shown, but excluded from the "who references it" scan.
GENERIC_SYMBOLS = {"logger", "log", "prisma", "redis", "router", "app",
                   "server", "db", "client", "config", "utils", "helper",
                   "index", "request", "response", "req", "res", "express"}


def extract_symbols(content: str, path: str) -> list[str]:
    """Symbols/routes defined in one file. Cheap regex scan of a single file."""
    if path.endswith(".py"):
        syms = [g for m in PY_SYMBOL.finditer(content) for g in m.groups() if g]
    else:
        syms = [g for m in TS_SYMBOL.finditer(content) for g in m.groups() if g]
    return sorted(set(syms))


def extract_imports(content: str) -> list[str]:
    return IMPORT_RE.findall(content)


def repo_root_hint(repo: str) -> str | None:
    """Return a local path for the PR's repo if we can find it cheaply.

    Checks, in order:
      1. current directory is inside a git repo whose remote matches `repo`
      2. DEEPIRI_QA_WORKSPACE env var (QA's checked-out clone root)
      3. sibling/child dirs named after the repo's short name
    """
    short = repo.split("/")[-1]
    candidates: list[str] = []
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        candidates.append(top)
        # If the current repo is the platform monorepo, the PR's repo may be a
        # submodule nested deeper — resolve it via .gitmodules (cheap, exact).
        gm = os.path.join(top, ".gitmodules")
        if os.path.isfile(gm):
            r = subprocess.run(
                ["git", "config", "--file", gm, "--get-regexp",
                 r"submodule\..*\.(path|url)"],
                capture_output=True, text=True,
            )
            for line in r.stdout.splitlines():
                if short not in line:
                    continue
                parts = line.split()
                if len(parts) >= 2 and parts[0].endswith(".path"):
                    candidates.append(os.path.join(top, parts[1]))
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    ws = os.environ.get("DEEPIRI_QA_WORKSPACE")
    if ws:
        candidates.append(ws)
        candidates.append(os.path.join(ws, short))
    candidates.append(short)
    candidates.append(f"./{short}")
    for c in candidates:
        if not c or not os.path.isdir(c):
            continue
        gitdir = os.path.join(c, ".git")
        if os.path.isdir(gitdir) or os.path.isfile(gitdir):
            r = subprocess.run(
                ["git", "-C", c, "remote", "get-url", "origin"],
                capture_output=True, text=True,
            )
            if r.returncode == 0 and short in r.stdout:
                return c
    return None


def git_rev(repo_path: str, ref: str) -> str | None:
    r = subprocess.run(["git", "-C", repo_path, "rev-parse", "--verify", ref],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def git_show_file(repo_path: str, rev: str, path: str) -> str | None:
    """Read a file at a specific rev WITHOUT checking anything out (non-destructive)."""
    r = subprocess.run(["git", "-C", repo_path, "show", f"{rev}:{path}"],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def detect_js_test_runner(repo_path: str) -> str | None:
    pkg = os.path.join(repo_path, "package.json")
    if not os.path.isfile(pkg):
        return None
    try:
        with open(pkg) as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
    test_script = (data.get("scripts", {}) or {}).get("test", "")
    for runner in ("vitest", "jest", "mocha", "ava"):
        if runner in test_script:
            return runner
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    for runner in ("vitest", "jest", "mocha", "ava"):
        if runner in deps:
            return runner
    return None


def test_command_for(cd_path: str, rel_path: str, runner: str | None) -> str:
    """A concrete, runnable command for one specific test file — not the
    generic per-area `npm test`."""
    if rel_path.endswith(".py"):
        return f"cd {cd_path} && pytest {rel_path} -v"
    if runner == "vitest":
        return f"cd {cd_path} && npx vitest run {rel_path}"
    if runner == "mocha":
        return f"cd {cd_path} && npx mocha {rel_path}"
    if runner == "ava":
        return f"cd {cd_path} && npx ava {rel_path}"
    # default to jest — the most common runner across these services, and a
    # safe guess when package.json couldn't be read
    return f"cd {cd_path} && npx jest {rel_path}"


def rg_files(repo_path: str, pattern: str, extra_globs: list[str] | None = None) -> list[str]:
    """Files containing `pattern`, via ripgrep (gitignored dirs auto-skipped)."""
    cmd = ["rg", "-l", "--no-messages", "-g", "!node_modules", "-g", "!*.lock",
           "--glob", "*.{py,ts,tsx,js,jsx}", pattern, repo_path]
    if extra_globs:
        for g in extra_globs:
            cmd.append("-g")
            cmd.append(g)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode not in (0, 1):
        return []
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def deep_analyze(repo: str, pr: dict, files: list[dict], workspace: str | None) -> dict:
    """Scan the local repo for what specifically exercises the changed code."""
    short = repo.split("/")[-1]
    root = repo_root_hint(repo)
    head_sha = pr.get("head", {}).get("sha")
    if not root or head_sha is None:
        return {"available": False, "reason": "no_local_repo",
                "clone_hint": f"git clone https://github.com/{repo}.git && "
                              f"git -C {short} fetch origin {pr.get('head', {}).get('ref', '')}"}

    if git_rev(root, head_sha) is None:
        r = subprocess.run(["git", "-C", root, "fetch", "origin",
                            pr.get("head", {}).get("ref", "")],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return {"available": False, "reason": "fetch_failed",
                    "clone_hint": f"git fetch origin {pr.get('head', {}).get('ref', '')}"}

    cd_path = os.path.relpath(root, os.getcwd()) or "."
    js_runner = detect_js_test_runner(root)
    changed = []
    for f in files:
        path = f["filename"]
        if not path.endswith((".py", ".ts", ".tsx", ".js", ".jsx")):
            continue
        content = git_show_file(root, head_sha, path)
        if content is None:
            continue
        syms = extract_symbols(content, path)
        imports = extract_imports(content)
        # one import hop: what do the changed files themselves touch?
        neighbors = []
        for imp in imports:
            base = imp.split("/")[-1].removesuffix(".py")
            if base and base not in (".", ".."):
                hits = rg_files(root, rf"(from|import|require)\('?[^'\"\n]*{re.escape(base)}['\"]?")
                neighbors += [h for h in hits if h.split("/")[-1] != path.split("/")[-1]]
        # reverse hop: who references the changed symbols (tests + consumers)?
        users = []
        for s in syms:
            if s in GENERIC_SYMBOLS:
                continue
            users += rg_files(root, rf"\b{re.escape(s)}\b")
        base = path.split("/")[-1]
        def relpath(u: str) -> str:
            return os.path.relpath(u, root)
        users = sorted(set(relpath(u) for u in users if u.split("/")[-1] != base))[:12]
        neighbors = sorted(set(relpath(n) for n in neighbors))[:8]
        test_files = sorted(set(u for u in users if any(m in u for m in TEST_MARKERS)))[:6]
        changed.append({
            "path": path,
            "symbols": syms,
            "imports": imports,
            "neighbors": neighbors,
            "users": users,
            "tests": test_files,
            "test_commands": [test_command_for(cd_path, t, js_runner) for t in test_files],
        })

    return {"available": True, "repo_path": root, "head_sha": head_sha,
            "files": changed}


def render_deep(deep: dict) -> list[str]:
    lines = []
    if not deep.get("available"):
        lines.append("> **Deep scan:** no local checkout of this repo found. "
                     f"Clone it first so the plan can find what to test:\n>\n"
                     f"> ```bash\n> {deep.get('clone_hint')}\n> ```")
        return lines
    lines.append("#### What to test (deep scan of local code)")
    lines.append("")
    lines.append(f"_Scanned `{deep.get('repo_path')}` at `{deep.get('head_sha', '')[:8]}` "
                 "— only changed files parsed, references found via ripgrep._")
    lines.append("")
    any_hits = False
    for cf in deep.get("files", []):
        if not (cf["symbols"] or cf["neighbors"] or cf["users"] or cf["tests"]):
            continue
        any_hits = True
        lines.append(f"- **`{cf['path']}`**")
        if cf["symbols"]:
            lines.append(f"  - Changed symbols/routes: `{', '.join(cf['symbols'])}`")
        if cf["tests"]:
            lines.append("  - **Tests that exercise it — run these:**")
            for t, c in zip(cf["tests"], cf["test_commands"]):
                lines.append(f"    - `{t}`: `{c}`")
        if cf["users"]:
            lines.append(f"  - Code that references it (smoke these): `{', '.join(cf['users'])}`")
        if cf["neighbors"]:
            lines.append(f"  - Directly imports (regression-check these): `{', '.join(cf['neighbors'])}`")
    if not any_hits:
        lines.append("- No test/consumer references found for changed files — "
                     "manual QA coverage applies.")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def resolve_pr(repo: str, pr_arg: str | None) -> int:
    if pr_arg:
        return int(pr_arg)
    result = gh("pr", "view", "--repo", repo, "--json", "number", "--jq", ".number")
    if result.returncode == 0 and result.stdout.strip():
        return int(result.stdout.strip())
    raise SystemExit(f"No --pr given and no PR is open for the current branch in {repo}.")


def main():
    ap = argparse.ArgumentParser(description="PR QA Test Planner")
    ap.add_argument("--repo", help="OWNER/REPO (default: current gh repo)")
    ap.add_argument("--pr", type=int, help="PR number (default: current branch's PR)")
    ap.add_argument("--org", default=ORG, help=f"GitHub org (default: {ORG})")
    ap.add_argument("--json", action="store_true", help="print plan as JSON")
    ap.add_argument("--comment", action="store_true", help="post plan as PR comment")
    ap.add_argument("--deep", action="store_true",
                    help="scan local checkout to find exactly what to test")
    ap.add_argument("--workspace", help="dir to search for local clones "
                                        "(default: $DEEPIRI_QA_WORKSPACE)")
    ap.add_argument("--out", help="write markdown plan to this file")
    args = ap.parse_args()

    if not args.repo:
        r = gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        if r.returncode != 0:
            raise SystemExit("Could not detect repo; pass --repo OWNER/REPO")
        args.repo = r.stdout.strip()

    pr_number = resolve_pr(args.repo, args.pr)
    pr = gh_json(f"repos/{args.repo}/pulls/{pr_number}")

    files: list[dict] = []
    page = 1
    while True:
        batch = gh_json(f"repos/{args.repo}/pulls/{pr_number}/files?per_page=100&page={page}")
        if not batch:
            break
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    plan = analyze(files)
    plan["pr_url"] = pr.get("html_url")
    plan["repo"] = args.repo
    plan["referenced_prs"] = find_referenced_prs(args.repo, pr.get("body") or "",
                                                 pr.get("number", 0))
    plan["submodule_bump_details"] = resolve_submodule_bumps(files, args.org)
    if args.deep:
        plan["deep"] = deep_analyze(args.repo, pr, files, args.workspace)

    if args.json:
        print(json.dumps(plan, indent=2))
        return

    md = render_markdown(plan, pr, args.repo)
    print(md)

    if args.out:
        with open(args.out, "w") as f:
            f.write(md + "\n")
        print(f"\n[written] {args.out}", file=sys.stderr)

    if args.comment:
        body = md + "\n\n---\n_Comment generated by `deepiri-suite/scripts/pr-qa-planner.py`._"
        c = gh("pr", "comment", "--repo", args.repo, str(pr_number), "--body", body)
        if c.returncode != 0:
            raise SystemExit(f"Failed to post comment: {c.stderr.strip()}")
        print(f"\n[posted] QA plan as comment on {pr.get('html_url')}", file=sys.stderr)


if __name__ == "__main__":
    main()
