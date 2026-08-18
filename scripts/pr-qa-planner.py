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
    if plan["submodule_bumps"]:
        lines.append("- **Submodule bumps:** this PR only moves submodule pointers — "
                     "each bumped service must be tested in the integrated stack, and "
                     "check the submodule repo for its own open PR at the new commit.")
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
