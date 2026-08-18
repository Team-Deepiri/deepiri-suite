#!/usr/bin/env python3
"""
PR Review Quality Enforcement for Team-Deepiri.

The QA review-submission rule (see the deepiri-qa-workflow skill and
scripts/pr-qa-planner.py) requires every Approve/Request-Changes review to
report what was actually tested, in this shape:

    Environment: [qa-team stack, build vs start]
    Health check: [all containers healthy / issues]
    Sorge pass: [what it flagged, how you handled it]
    Manual testing: [what you exercised, frontend/backend]
    Automated tests: [run / not run, why]

Nothing currently checks that a review actually filled this in rather than
leaving the bracketed placeholders, skipping sections, or rubber-stamping
with a bare "LGTM". This script does that check, either for one PR or swept
across a repo's recent PRs to build a compliance report per reviewer.

Usage:
    # Check the latest Approve/Request-Changes review on one PR
    python3 scripts/pr-review-quality-check.py --repo Team-Deepiri/deepiri-auth-service --pr 74

    # Nudge the reviewer with a PR comment if their review is incomplete
    python3 scripts/pr-review-quality-check.py --repo Team-Deepiri/deepiri-auth-service --pr 74 --comment

    # Sweep a repo's last N closed PRs and report compliance per reviewer
    python3 scripts/pr-review-quality-check.py --repo Team-Deepiri/deepiri-auth-service --sweep --limit 50

    # Sweep every repo the platform's QA team touches
    python3 scripts/pr-review-quality-check.py --sweep --all-repos --limit 30

Requires the `gh` CLI authenticated for the org. Stdlib only.
"""
import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict

ORG = "Team-Deepiri"

# Repos the QA team actually reviews — kept in sync with pr-qa-planner.py's
# SERVICE_MAP short names so --all-repos covers the same surface.
ALL_REPOS = [
    "deepiri-platform", "deepiri-api-gateway", "deepiri-auth-service",
    "deepiri-language-intelligence-service", "deepiri-external-bridge-service",
    "deepiri-messaging-service", "deepiri-realtime-gateway", "deepiri-telemetry",
    "deepiri-registry", "deepiri-jobs", "deepiri-truss", "deepiri-speech",
    "deepiri-workflow-orchestrator", "deepiri-communications-hub",
    "deepiri-decision-intelligence", "deepiri-adaptive-experience-engine",
    "deepiri-incentive-engine", "deepiri-prismpipe", "deepiri-synapse",
    "deepiri-sugar-glider", "deepiri-shared-utils", "deepiri-web-frontend",
    "deepiri-modelkit", "deepiri-logger", "deepiri-ollama-utils",
    "deepiri-core-api", "diri-cyrex", "diri-helox", "diri-persola",
    "deepiri-suite",
]

# The five sections the review-submission template requires, in the order
# they appear. Matching is case-insensitive and tolerant of minor rewording
# (e.g. "Health Check" vs "Health check").
REQUIRED_SECTIONS = [
    "Environment",
    "Health check",
    "Sorge pass",
    "Manual testing",
    "Automated tests",
]

# A section "counts" only if there's real content after the colon — not left
# as the literal bracketed placeholder, not empty, and not a single filler
# word that says nothing (e.g. "n/a" with no reason, "done", "ok").
PLACEHOLDER_RE = re.compile(r"^\[.*\]$")
EMPTY_FILLER_RE = re.compile(r"^(n/?a|none|done|ok|yes|no|-|—)\.?$", re.IGNORECASE)

FINAL_STATES = {"APPROVED", "CHANGES_REQUESTED"}


def gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh"] + list(args), capture_output=True, text=True)


def fetch_reviews(repo: str, pr: int) -> list[dict]:
    r = gh("api", f"repos/{repo}/pulls/{pr}/reviews", "--paginate",
           "--jq", ".[] | {id,user:.user.login,state,body,submitted_at,html_url:.html_url}")
    if r.returncode != 0:
        raise RuntimeError(f"gh api repos/{repo}/pulls/{pr}/reviews failed: "
                           f"{r.stderr.strip()}")
    reviews = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            reviews.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return reviews


def evaluate_review(body: str) -> dict:
    """Check one review body against the required template.

    Returns {"present": {section: text or None}, "missing": [...],
    "placeholder": [...], "empty_filler": [...], "compliant": bool}
    """
    body = body or ""
    present, missing, placeholder, empty_filler = {}, [], [], []
    for section in REQUIRED_SECTIONS:
        m = re.search(rf"^\s*{re.escape(section)}\s*:\s*(.+)$", body,
                      re.IGNORECASE | re.MULTILINE)
        if not m:
            missing.append(section)
            present[section] = None
            continue
        value = m.group(1).strip()
        present[section] = value
        if not value or PLACEHOLDER_RE.match(value):
            placeholder.append(section)
        elif EMPTY_FILLER_RE.match(value):
            empty_filler.append(section)
    compliant = not missing and not placeholder and not empty_filler
    return {"present": present, "missing": missing, "placeholder": placeholder,
            "empty_filler": empty_filler, "compliant": compliant}


def check_pr(repo: str, pr: int) -> dict:
    reviews = fetch_reviews(repo, pr)
    final_reviews = [r for r in reviews if r["state"] in FINAL_STATES]
    comment_only = [r for r in reviews if r["state"] == "COMMENTED"]

    if not final_reviews:
        return {
            "repo": repo, "pr": pr, "verdict": "no_final_review",
            "detail": (f"{len(comment_only)} comment-only review(s), no "
                       "Approve/Request Changes yet" if comment_only
                       else "no reviews submitted yet"),
            "reviews": [],
        }

    results = []
    for r in final_reviews:
        ev = evaluate_review(r["body"])
        results.append({**r, "eval": ev})

    latest = results[-1]
    return {
        "repo": repo, "pr": pr,
        "verdict": "compliant" if latest["eval"]["compliant"] else "incomplete",
        "reviews": results,
    }


def render_report(result: dict) -> str:
    lines = [f"### Review quality check — {result['repo']}#{result['pr']}", ""]
    if result["verdict"] == "no_final_review":
        lines.append(f"⚠️ {result['detail']}.")
        return "\n".join(lines)

    for r in result["reviews"]:
        ev = r["eval"]
        icon = "✅" if ev["compliant"] else "❌"
        lines.append(f"{icon} **{r['user']}** — {r['state']} ({r['submitted_at']})")
        if ev["missing"]:
            lines.append(f"  - Missing section(s): {', '.join(ev['missing'])}")
        if ev["placeholder"]:
            lines.append(f"  - Left as placeholder/empty: {', '.join(ev['placeholder'])}")
        if ev["empty_filler"]:
            lines.append(f"  - Filler with no substance (e.g. \"n/a\", \"done\"): "
                         f"{', '.join(ev['empty_filler'])}")
        if ev["compliant"]:
            lines.append("  - All 5 sections present with real content")
    return "\n".join(lines)


NUDGE_TEMPLATE = """Hey @{user} — this review is missing some of the required test-report sections from the [QA review guide]({guide_url}). Could you fill these in before this counts as a final review?

{missing_md}

Template:
```text
Environment: [qa-team stack, build vs start]
Health check: [all containers healthy / issues]
Sorge pass: [what it flagged, how you handled it]
Manual testing: [what you exercised, frontend/backend]
Automated tests: [run / not run, why]
```

_Automated nudge from `deepiri-suite/scripts/pr-review-quality-check.py`._"""

GUIDE_URL = "https://docs.google.com/document/d/1Qc2XyFIlU9cLuHWbV6GBvM7NLwY2tCGAwdvY6Bcm0PM/edit"


def post_nudge(repo: str, pr: int, review: dict) -> None:
    ev = review["eval"]
    gaps = []
    if ev["missing"]:
        gaps.append(f"- Missing entirely: {', '.join(ev['missing'])}")
    if ev["placeholder"]:
        gaps.append(f"- Still the template placeholder / empty: {', '.join(ev['placeholder'])}")
    if ev["empty_filler"]:
        gaps.append(f"- No real substance: {', '.join(ev['empty_filler'])}")
    body = NUDGE_TEMPLATE.format(user=review["user"], guide_url=GUIDE_URL,
                                 missing_md="\n".join(gaps))
    c = gh("pr", "comment", "--repo", repo, str(pr), "--body", body)
    if c.returncode != 0:
        print(f"[warn] failed to post nudge: {c.stderr.strip()}", file=sys.stderr)
    else:
        print(f"[posted] nudge comment on {repo}#{pr}", file=sys.stderr)


def sweep(repo: str, limit: int) -> list[dict]:
    r = gh("pr", "list", "--repo", repo, "--state", "closed",
           "--limit", str(limit), "--json", "number,mergedAt")
    if r.returncode != 0:
        print(f"[warn] skipping {repo}: gh pr list failed: {r.stderr.strip()}",
              file=sys.stderr)
        return []
    try:
        prs = json.loads(r.stdout)
    except json.JSONDecodeError:
        print(f"[warn] skipping {repo}: unparseable PR list", file=sys.stderr)
        return []
    results = []
    for p in prs:
        if not p.get("mergedAt"):
            continue
        try:
            results.append(check_pr(repo, p["number"]))
        except RuntimeError as e:
            print(f"[warn] skipping {repo}#{p['number']}: {e}", file=sys.stderr)
    return results


def render_sweep_report(results: list[dict], repos: list[str]) -> str:
    by_reviewer: dict[str, dict] = defaultdict(lambda: {"compliant": 0, "incomplete": 0, "no_final_review": 0})
    for res in results:
        if res["verdict"] == "no_final_review":
            by_reviewer["(unassigned/no review)"]["no_final_review"] += 1
            continue
        latest = res["reviews"][-1]
        key = f"{res['verdict']}"
        by_reviewer[latest["user"]][res["verdict"]] += 1

    lines = [f"## Review quality sweep — {', '.join(repos)}", ""]
    lines.append(f"- **PRs checked:** {len(results)}")
    compliant_n = sum(1 for r in results if r["verdict"] == "compliant")
    incomplete_n = sum(1 for r in results if r["verdict"] == "incomplete")
    missing_n = sum(1 for r in results if r["verdict"] == "no_final_review")
    lines.append(f"- **Compliant:** {compliant_n}  ·  **Incomplete:** {incomplete_n}  "
                 f"·  **No final review found:** {missing_n}")
    lines.append("")
    lines.append("| Reviewer | Compliant | Incomplete | No final review | Compliance rate |")
    lines.append("|----------|-----------|------------|------------------|------------------|")
    for reviewer, counts in sorted(
            by_reviewer.items(),
            key=lambda kv: -(kv[1]["compliant"] + kv[1]["incomplete"] + kv[1]["no_final_review"])):
        c, i, n = counts["compliant"], counts["incomplete"], counts["no_final_review"]
        total = c + i
        rate = f"{100 * c // total}%" if total else "—"
        lines.append(f"| {reviewer} | {c} | {i} | {n} | {rate} |")
    lines.append("")
    if by_reviewer.get("(unassigned/no review)", {}).get("no_final_review"):
        lines.append("_\"(unassigned/no review)\" = merged PRs with no Approve/Request "
                     "Changes review at all (e.g. admin-merged, no QA pass)._")
        lines.append("")

    if incomplete_n:
        lines.append("### Incomplete reviews — detail")
        lines.append("")
        for res in results:
            if res["verdict"] != "incomplete":
                continue
            latest = res["reviews"][-1]
            ev = latest["eval"]
            gaps = ev["missing"] + ev["placeholder"] + ev["empty_filler"]
            lines.append(f"- [{res['repo']}#{res['pr']}]({latest['html_url']}) — "
                         f"**{latest['user']}**: {', '.join(sorted(set(gaps)))}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="PR Review Quality Enforcement")
    ap.add_argument("--repo", help="OWNER/REPO or bare repo name (default org: "
                                   f"{ORG})")
    ap.add_argument("--pr", type=int, help="PR number to check")
    ap.add_argument("--comment", action="store_true",
                    help="post a nudge comment on the PR if the latest final "
                         "review is incomplete")
    ap.add_argument("--sweep", action="store_true",
                    help="scan recent merged PRs instead of checking one")
    ap.add_argument("--all-repos", action="store_true",
                    help="sweep every repo in ALL_REPOS (implies --sweep)")
    ap.add_argument("--limit", type=int, default=30,
                    help="PRs per repo to sweep (default: 30)")
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    args = ap.parse_args()

    def full_repo(name: str) -> str:
        return name if "/" in name else f"{ORG}/{name}"

    if args.all_repos:
        args.sweep = True

    if args.sweep:
        if not args.all_repos and not args.repo:
            raise SystemExit("--sweep needs --repo or --all-repos")
        repos = [full_repo(r) for r in ALL_REPOS] if args.all_repos else [full_repo(args.repo)]
        all_results = []
        for repo in repos:
            print(f"[scan] {repo} (last {args.limit} merged PRs)...", file=sys.stderr)
            all_results.extend(sweep(repo, args.limit))
        if args.json:
            print(json.dumps(all_results, indent=2))
        else:
            print(render_sweep_report(all_results, repos))
        return

    if not args.repo or not args.pr:
        raise SystemExit("Single-PR mode needs --repo and --pr (or use --sweep)")
    repo = full_repo(args.repo)
    try:
        result = check_pr(repo, args.pr)
    except RuntimeError as e:
        raise SystemExit(str(e))

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(render_report(result))

    if args.comment and result["verdict"] == "incomplete":
        post_nudge(repo, args.pr, result["reviews"][-1])


if __name__ == "__main__":
    main()
