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

Also does PR review-quality enforcement — checks that an Approve/Request
Changes review actually filled in the required test-report template
(Environment / Health check / Sorge pass / Manual testing / Automated tests)
instead of rubber-stamping with a bare "LGTM":

    python3 scripts/pr-qa-planner.py --review-check --repo Team-Deepiri/deepiri-auth-service --pr 74
    python3 scripts/pr-qa-planner.py --review-check --sweep --all-repos --limit 30

Options:
    --repo OWNER/REPO   Repository to analyze (default: current gh repo).
    --pr NUMBER         PR number (default: PR for the current branch, if any).
    --json              Print the machine-readable plan as JSON.
    --comment           Post the test plan as a comment on the PR.
    --out FILE          Also write the markdown plan to FILE.
    --org NAME          GitHub org owning the repos (default: Team-Deepiri).
    --deep              Scan a local checkout to find exactly what to test.
    --skip-setup        Skip the automatic gh CLI install/auth check.

    --review-check      Check review-submission quality instead of planning.
    --sweep             (with --review-check) scan recent merged PRs, not one.
    --all-repos         (with --sweep) scan every repo the QA team reviews.
    --limit N           (with --sweep) PRs per repo to scan (default: 30).
    --nudge             (with --review-check) post a PR comment nudging the
                        reviewer if their review is incomplete.

No manual setup required: on first run this script checks for the `gh` CLI
and auto-installs it via the platform's package manager if missing, then
runs `gh auth login` if not authenticated. Stdlib only otherwise.
"""
import argparse
import itertools
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import defaultdict

ORG = "Team-Deepiri"


# ---------------------------------------------------------------------------
# QA ASSIST — terminal UI: blue/teal banner, spinner, interactive menu.
# Colors degrade gracefully (empty strings) when stdout isn't a real
# terminal — plain output for CI logs, --json, or piping into a file.
# ---------------------------------------------------------------------------

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

def _c(code: str) -> str:
    return code if _COLOR else ""

BLUE = _c("\033[38;2;64;140;255m")
TEAL = _c("\033[38;2;0;191;179m")
BOLD = _c("\033[1m")
DIM = _c("\033[2m")
RESET = _c("\033[0m")


def print_banner() -> None:
    if not _COLOR:
        print("QA ASSIST — Team-Deepiri PR QA toolkit", file=sys.stderr)
        return
    title = "Q A   A S S I S T"
    subtitle = "Team-Deepiri  ·  PR QA Toolkit"
    width = max(len(title), len(subtitle)) + 6

    def centered(s: str) -> str:
        return s.center(width)

    top = "╭" + "─" * width + "╮"
    mid = "├" + "─" * width + "┤"
    bot = "╰" + "─" * width + "╯"
    print(f"{BLUE}{top}{RESET}")
    print(f"{BLUE}│{RESET}{TEAL}{BOLD}{centered(title)}{RESET}{BLUE}│{RESET}")
    print(f"{BLUE}{mid}{RESET}")
    print(f"{BLUE}│{RESET}{DIM}{centered(subtitle)}{RESET}{BLUE}│{RESET}")
    print(f"{BLUE}{bot}{RESET}")


class Spinner:
    """Minimal stdlib spinner — no dependency beyond threading/time. Silent
    when stdout isn't a TTY (CI logs stay clean, no braille noise)."""
    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, message: str):
        self.message = message
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _spin(self) -> None:
        for frame in itertools.cycle(self.FRAMES):
            if self._stop.is_set():
                break
            sys.stderr.write(f"\r{TEAL}{frame}{RESET} {self.message}{' ' * 10}")
            sys.stderr.flush()
            time.sleep(0.08)
        sys.stderr.write("\r" + " " * (len(self.message) + 12) + "\r")
        sys.stderr.flush()

    def __enter__(self) -> "Spinner":
        if _COLOR:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            print(f"[...] {self.message}", file=sys.stderr)
        return self

    def __exit__(self, *exc) -> None:
        if self._thread:
            self._stop.set()
            self._thread.join()


def maybe_install_opencode(skip: bool = False) -> None:
    """Optional: ask once whether to also install opencode (the open-source
    terminal AI coding agent, https://opencode.ai) alongside gh/dtm. Purely
    opt-in — only prompts on a real TTY, never in CI or --skip-setup runs."""
    if skip or not sys.stdin.isatty() or not sys.stdout.isatty():
        return
    if shutil.which("opencode"):
        return
    try:
        answer = input(f"{TEAL}?{RESET} Also install {BOLD}opencode{RESET} "
                       "(open-source AI coding CLI, https://opencode.ai)? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if answer not in ("y", "yes"):
        return
    with Spinner("Installing opencode..."):
        if shutil.which("npm"):
            r = subprocess.run(["npm", "install", "-g", "opencode-ai@latest"],
                               capture_output=True, text=True)
        else:
            r = subprocess.run(["bash", "-c", "curl -fsSL https://opencode.ai/install | bash"],
                               capture_output=True, text=True)
    if r.returncode == 0:
        print(f"{TEAL}✓{RESET} opencode installed.", file=sys.stderr)
    else:
        print(f"[warn] opencode install failed: {r.stderr.strip()}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Dependency auto-setup: no manual "install gh, run gh auth login" steps.
# ---------------------------------------------------------------------------

def ensure_gh_ready(skip: bool = False) -> None:
    """Make sure the `gh` CLI is installed and authenticated before we rely on
    it for everything else in this script."""
    if skip:
        return
    if shutil.which("gh") is None:
        print("[setup] GitHub CLI ('gh') not found — attempting to install it...",
              file=sys.stderr)
        if not _install_gh():
            raise SystemExit(
                "Could not auto-install the GitHub CLI for this platform. "
                "Install it manually: https://cli.github.com then re-run."
            )
        if shutil.which("gh") is None:
            raise SystemExit(
                "gh installed but isn't on PATH yet — open a new shell "
                "(or re-source your profile) and re-run this script."
            )
    r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if r.returncode != 0:
        print("[setup] gh CLI is not authenticated — launching `gh auth login`...",
              file=sys.stderr)
        login = subprocess.run(["gh", "auth", "login"])
        if login.returncode != 0:
            raise SystemExit("`gh auth login` did not complete. Run it manually "
                             "and re-run this script.")


def _install_gh() -> bool:
    """Best-effort install of the gh CLI via the current platform's package
    manager. Falls back through managers in order until one works."""
    system = platform.system()
    try:
        if system == "Darwin" and shutil.which("brew"):
            return subprocess.run(["brew", "install", "gh"]).returncode == 0
        if system == "Linux":
            if shutil.which("apt-get"):
                if subprocess.run(["sudo", "apt-get", "install", "-y", "gh"]).returncode == 0:
                    return True
                # Not in the default repos on this release — add the official
                # apt source (per https://cli.github.com/) and retry.
                setup = (
                    "curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg "
                    "| sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && "
                    "sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && "
                    'echo "deb [arch=$(dpkg --print-architecture) '
                    "signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] "
                    'https://cli.github.com/packages stable main" | '
                    "sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null && "
                    "sudo apt-get update && sudo apt-get install -y gh"
                )
                return subprocess.run(["bash", "-c", setup]).returncode == 0
            if shutil.which("dnf"):
                return subprocess.run(["sudo", "dnf", "install", "-y", "gh"]).returncode == 0
            if shutil.which("yum"):
                return subprocess.run(["sudo", "yum", "install", "-y", "gh"]).returncode == 0
            if shutil.which("pacman"):
                return subprocess.run(["sudo", "pacman", "-S", "--noconfirm", "github-cli"]).returncode == 0
            if shutil.which("snap"):
                return subprocess.run(["sudo", "snap", "install", "gh"]).returncode == 0
        if system == "Windows" and shutil.which("winget"):
            return subprocess.run(["winget", "install", "--id", "GitHub.cli"]).returncode == 0
    except (subprocess.CalledProcessError, OSError):
        return False
    return False

# ---------------------------------------------------------------------------
# Service map: path prefix -> area. In the platform monorepo a submodule bump
# (a one-line gitlink diff) means "this sub-service changed"; a path under a
# service's tree means the change lives inside that service repo directly.
#
# This used to be a hand-maintained list of every repo + its test command —
# which silently goes stale the moment a service is added, renamed, or moves.
# Instead we source it live, in priority order:
#
#   1. dtm (deepiri-pkg-version-manager) — if installed (auto-installed below
#      if missing), `dtm scan` builds a real dependency graph of the local
#      platform checkout: exact repo_path, package_type (npm/poetry/pip) per
#      package. This is the most precise source and needs no guessing.
#   2. .gitmodules + GitHub repo language — works from anywhere with zero
#      local checkout (fetched via the GitHub API), for CI or a machine that
#      doesn't have deepiri-platform cloned. Less precise (language-based
#      guess at the test command instead of dtm's exact package_type).
#
# (deepiri-vizult was also considered — it maps runtime service topology from
# docker-compose/k8s/source scanning, which is the right tool for "what talks
# to what", not "which repo does this path belong to". dtm's package/path
# registry is the closer fit for this script's needs.)
# ---------------------------------------------------------------------------

TEST_CMD_BY_TYPE = {
    "npm": "npm test",
    "poetry": "poetry run pytest",
    "pip": "pytest",
}

# Paths that are monorepo structure, not a versioned package dtm would know
# about — kept as a small fixed list since these aren't "services" in the
# sense Joe's comment was about (nothing here duplicates a repo/test-command
# registry that could go stale).
LOCAL_INFRA_PATHS = [
    ("deploy/", "Deployment/K8s", "helm lint + kubectl apply --dry-run=client"),
    ("ops/", "Ops", "cd ops && make lint"),
    ("skaffold/", "Skaffold", "skaffold render --validate"),
    ("teams/", "Team Dev Environments", "manual smoke of affected team env"),
    ("scripts/", "Repo Scripts", "python3 -m py_compile <changed scripts>"),
    (".github/workflows/", "CI Workflows", "PR CI run"),
]


def get_dtm_db_path() -> "os.PathLike":
    return os.path.join(os.path.expanduser("~"), ".deepiri", "dtm.db")


def ensure_dtm_ready(skip: bool = False) -> bool:
    """Best-effort install of dtm (deepiri-pkg-version-manager) — the live
    dependency-graph tool that replaces a hand-maintained service map.
    Non-fatal: returns False so callers fall back to the .gitmodules path."""
    if skip:
        return False
    if shutil.which("dtm"):
        return True
    print("[setup] dtm (deepiri-pkg-version-manager) not found — attempting "
          "to install it...", file=sys.stderr)
    cache_dir = os.path.join(os.path.expanduser("~"), ".deepiri",
                             "deepiri-pkg-version-manager")
    try:
        if not os.path.isdir(cache_dir):
            r = subprocess.run(
                ["git", "clone", "--depth", "1",
                 f"https://github.com/{ORG}/deepiri-pkg-version-manager.git", cache_dir],
                capture_output=True, text=True)
            if r.returncode != 0:
                print(f"[warn] could not clone dtm: {r.stderr.strip()}", file=sys.stderr)
                return False
        if not _pip_install_cli(cache_dir, "dtm"):
            return False
    except OSError as e:
        print(f"[warn] dtm setup failed: {e}", file=sys.stderr)
        return False
    if shutil.which("dtm") is None:
        print("[warn] dtm installed but not on PATH — open a new shell (or "
              "re-source your profile) and re-run, or fall back to .gitmodules.",
              file=sys.stderr)
        return False
    return True


def _pip_install_cli(source_dir: str, binary_name: str) -> bool:
    """Install a local Python package as an isolated CLI tool. Modern Debian/
    Ubuntu's system Python refuses `pip install --user` (PEP 668 "externally
    managed environment"), so prefer pipx — it's built exactly for this
    (isolated venv per tool, puts the binary on PATH) — and fall back through
    plain --user pip, then --user --break-system-packages as a last resort."""
    if shutil.which("pipx"):
        r = subprocess.run(["pipx", "install", source_dir],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return True
        print(f"[warn] pipx install failed, trying pip: {r.stderr.strip()}",
              file=sys.stderr)
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--user", "-e", source_dir],
                       capture_output=True, text=True)
    if r.returncode == 0:
        return True
    if "externally-managed-environment" in (r.stderr or ""):
        print("[setup] system Python is externally managed — retrying with "
              "--break-system-packages (installs into --user site, not system)...",
              file=sys.stderr)
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--user",
                            "--break-system-packages", "-e", source_dir],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return True
    print(f"[warn] could not install {binary_name}: {r.stderr.strip()}", file=sys.stderr)
    return False


def query_dtm_packages() -> list[dict]:
    """Read dtm's live dependency-graph DB instead of a hardcoded list."""
    db_path = get_dtm_db_path()
    if not os.path.isfile(db_path):
        return []
    import sqlite3
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute("SELECT name, repo_path, package_type, git_url, is_submodule "
                    "FROM dependencies")
        rows = cur.fetchall()
        con.close()
    except sqlite3.Error:
        return []
    return [{"name": r[0], "repo_path": r[1], "type": r[2], "git_url": r[3],
             "is_submodule": bool(r[4])} for r in rows]


def prettify_service_name(short: str) -> str:
    name = re.sub(r"^(deepiri-|diri-)", "", short)
    acronyms = {"api", "ai", "ml", "ui", "db", "cli"}
    return " ".join(w.upper() if w in acronyms else w.capitalize()
                    for w in name.split("-"))


def build_service_map_from_dtm(platform_root: str) -> list[tuple[str, str, str]] | None:
    """(path_prefix, area_label, test_command) triples from a live dtm scan
    of the local platform checkout. Returns None if dtm isn't usable here."""
    if not ensure_dtm_ready():
        return None
    r = subprocess.run(["dtm", "scan", "--path", platform_root],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print(f"[warn] dtm scan failed: {r.stderr.strip()}", file=sys.stderr)
        return None
    packages = query_dtm_packages()
    if not packages:
        return None
    entries = []
    for p in packages:
        if not p["is_submodule"] or not p["repo_path"]:
            continue
        try:
            rel = os.path.relpath(p["repo_path"], platform_root)
        except ValueError:
            continue
        if rel.startswith(".."):
            continue
        short = (p["git_url"] or p["name"]).rstrip("/").split("/")[-1].removesuffix(".git")
        label = prettify_service_name(short)
        if "shared" in rel:
            label += " (shared)"
        cmd = TEST_CMD_BY_TYPE.get(p["type"], "manual")
        entries.append((rel, label, f"cd {rel} && {cmd}"))
    return entries or None


def fetch_gitmodules_text(org: str) -> str | None:
    """Fetch deepiri-platform's .gitmodules via the GitHub API — works with
    no local checkout, still a live read instead of a hardcoded list."""
    r = gh("api", f"repos/{org}/deepiri-platform/contents/.gitmodules",
           "--jq", ".content")
    if r.returncode != 0 or not r.stdout.strip():
        return None
    import base64
    try:
        return base64.b64decode(r.stdout.strip()).decode("utf-8", errors="replace")
    except (ValueError, UnicodeDecodeError):
        return None


def guess_test_command_via_language(repo: str) -> str:
    r = gh("api", f"repos/{repo}", "--jq", ".language")
    lang = (r.stdout or "").strip().strip('"').lower() if r.returncode == 0 else ""
    if lang == "python":
        return "pytest"
    if lang in ("typescript", "javascript"):
        return "npm test"
    if lang == "go":
        return "go test ./..."
    return "manual"


def build_service_map_from_gitmodules(org: str) -> list[tuple[str, str, str]]:
    """Fallback when dtm isn't available/installable: parse .gitmodules for
    path->repo, then guess the test command from the repo's GitHub language.
    Still fully dynamic — no per-repo list to maintain by hand."""
    text = fetch_gitmodules_text(org)
    if not text:
        return []
    entries = []
    for block in re.split(r"\[submodule ", text)[1:]:
        pm = re.search(r"path\s*=\s*(\S+)", block)
        um = re.search(r"url\s*=\s*(\S+)", block)
        if not (pm and um):
            continue
        path = pm.group(1).strip()
        short = um.group(1).strip().rstrip("/").split("/")[-1].removesuffix(".git")
        label = prettify_service_name(short)
        if path.startswith("platform-services/shared/"):
            label += " (shared)"
        cmd = guess_test_command_via_language(f"{org}/{short}")
        entries.append((path, label, f"cd {path} && {cmd}"))
    return entries


def build_service_map(org: str) -> list[tuple[str, str, str]]:
    """The live path->(area, test command) map used by classify_path, built
    fresh each run instead of hand-maintained. Tries dtm first (precise),
    falls back to .gitmodules + language guess (works anywhere)."""
    platform_root = repo_root_hint(f"{org}/deepiri-platform")
    if platform_root:
        from_dtm = build_service_map_from_dtm(platform_root)
        if from_dtm:
            return from_dtm
    return build_service_map_from_gitmodules(org)

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

def classify_path(path: str, service_map: list[tuple[str, str, str]]) -> tuple[str, str]:
    """Return (area_label, test_command) for a path, or (None, None) if unknown."""
    low = path.lower()
    for prefix, label, cmd in service_map:
        if low.startswith(prefix.lower()):
            return label, cmd
    for prefix, label, cmd in LOCAL_INFRA_PATHS:
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


def analyze(files: list[dict], service_map: list[tuple[str, str, str]],
            changed_files_before: int = 0) -> dict:
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
        label, cmd = classify_path(f["filename"], service_map)
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
# Review-quality enforcement: did the reviewer actually fill in the required
# test-report template, or rubber-stamp with a bare "LGTM"?
# ---------------------------------------------------------------------------

def discover_org_repos(org: str) -> list[str]:
    """Every repo in the org, live — no hardcoded list to fall out of sync
    when a repo is added, renamed, or archived."""
    r = gh("repo", "list", org, "--json", "name", "--limit", "300",
           "--no-archived")
    if r.returncode != 0:
        print(f"[warn] could not list repos for {org}: {r.stderr.strip()}",
              file=sys.stderr)
        return []
    try:
        return sorted(x["name"] for x in json.loads(r.stdout))
    except json.JSONDecodeError:
        return []

# The five sections the review-submission template (see render_markdown's
# Phase 5) requires, in order. Matching is case-insensitive and tolerant of
# minor rewording.
REQUIRED_SECTIONS = [
    "Environment",
    "Health check",
    "Sorge pass",
    "Manual testing",
    "Automated tests",
]

# A section "counts" only if there's real content after the colon — not the
# literal bracketed placeholder, not empty, and not a filler word that says
# nothing ("n/a", "done", "ok") with no reason given.
PLACEHOLDER_RE = re.compile(r"^\[.*\]$")
EMPTY_FILLER_RE = re.compile(r"^(n/?a|none|done|ok|yes|no|-|—)\.?$", re.IGNORECASE)

FINAL_STATES = {"APPROVED", "CHANGES_REQUESTED"}


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


def check_pr_review(repo: str, pr: int) -> dict:
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


def render_review_report(result: dict) -> str:
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

_Automated nudge from `deepiri-suite/scripts/pr-qa-planner.py --review-check`._"""

GUIDE_URL = "https://docs.google.com/document/d/1Qc2XyFIlU9cLuHWbV6GBvM7NLwY2tCGAwdvY6Bcm0PM/edit"


def post_review_nudge(repo: str, pr: int, review: dict) -> None:
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


def sweep_repo_reviews(repo: str, limit: int) -> list[dict]:
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
            results.append(check_pr_review(repo, p["number"]))
        except RuntimeError as e:
            print(f"[warn] skipping {repo}#{p['number']}: {e}", file=sys.stderr)
    return results


def render_review_sweep_report(results: list[dict], repos: list[str]) -> str:
    by_reviewer: dict[str, dict] = defaultdict(
        lambda: {"compliant": 0, "incomplete": 0, "no_final_review": 0})
    for res in results:
        if res["verdict"] == "no_final_review":
            by_reviewer["(unassigned/no review)"]["no_final_review"] += 1
            continue
        latest = res["reviews"][-1]
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


def run_review_check(args) -> None:
    def full_repo(name: str) -> str:
        return name if "/" in name else f"{args.org}/{name}"

    if args.sweep or args.all_repos:
        if not args.all_repos and not args.repo:
            raise SystemExit("--review-check --sweep needs --repo or --all-repos")
        repos = ([full_repo(r) for r in discover_org_repos(args.org)]
                if args.all_repos else [full_repo(args.repo)])
        all_results = []
        for repo in repos:
            print(f"[scan] {repo} (last {args.limit} merged PRs)...", file=sys.stderr)
            all_results.extend(sweep_repo_reviews(repo, args.limit))
        if args.json:
            print(json.dumps(all_results, indent=2))
        else:
            print(render_review_sweep_report(all_results, repos))
        return

    if not args.repo or not args.pr:
        raise SystemExit("--review-check needs --repo and --pr (or use --sweep/--all-repos)")
    repo = full_repo(args.repo)
    try:
        result = check_pr_review(repo, args.pr)
    except RuntimeError as e:
        raise SystemExit(str(e))

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(render_review_report(result))

    if args.nudge and result["verdict"] == "incomplete":
        post_review_nudge(repo, args.pr, result["reviews"][-1])


# ---------------------------------------------------------------------------
# Interactive report viewer — the terminal experience for the plan, instead
# of just dumping markdown. Parses render_markdown()'s own output (single
# source of truth, no duplicated formatting logic) into scroll/checkbox
# lines. Falls back to a flat print if curses isn't available or stdout
# isn't a real terminal (CI, piping, redirected output).
# ---------------------------------------------------------------------------

CHECKBOX_LINE_RE = re.compile(r"^(\s*-\s*)\[([ xX])\](.*)$")


def _parse_report_lines(md: str) -> list[dict]:
    lines = []
    for raw in md.split("\n"):
        m = CHECKBOX_LINE_RE.match(raw)
        if m:
            prefix, mark, rest = m.groups()
            lines.append({"kind": "checkbox", "checked": mark.lower() == "x",
                         "prefix": prefix, "rest": rest, "raw": raw})
        elif raw.startswith("#"):
            lines.append({"kind": "header", "raw": raw})
        else:
            lines.append({"kind": "text", "raw": raw})
    return lines


def _render_report_line(line: dict) -> str:
    if line["kind"] == "checkbox":
        mark = "x" if line["checked"] else " "
        return f"{line['prefix']}[{mark}]{line['rest']}"
    return line["raw"]


def run_interactive_report(md: str, export_path: str) -> bool:
    """Curses checklist viewer over the generated plan. Returns True if it
    ran (caller shouldn't also flat-print); False if curses isn't usable
    here and the caller should fall back to a plain print."""
    try:
        import curses
    except ImportError:
        return False

    lines = _parse_report_lines(md)
    checkbox_idx = [i for i, l in enumerate(lines) if l["kind"] == "checkbox"]

    def loop(stdscr):
        curses.curs_set(0)
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_BLUE, -1)
        curses.init_pair(3, curses.COLOR_GREEN, -1)
        teal_attr = curses.color_pair(1) | curses.A_BOLD
        header_attr = curses.color_pair(2) | curses.A_BOLD
        checked_attr = curses.color_pair(3)

        top = 0
        cursor = 0 if checkbox_idx else -1
        status = ""

        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            body_h = max(1, h - 2)
            cur_line = checkbox_idx[cursor] if cursor >= 0 else -1
            if cur_line >= 0:
                if cur_line < top:
                    top = cur_line
                elif cur_line >= top + body_h:
                    top = cur_line - body_h + 1

            for row in range(body_h):
                li = top + row
                if li >= len(lines):
                    break
                line = lines[li]
                text = _render_report_line(line)
                attr = curses.A_NORMAL
                if line["kind"] == "header":
                    attr = header_attr
                elif line["kind"] == "checkbox":
                    attr = checked_attr if line["checked"] else curses.A_NORMAL
                    if li == cur_line:
                        attr |= curses.A_REVERSE
                try:
                    stdscr.addnstr(row, 0, text, max(0, w - 1), attr)
                except curses.error:
                    pass

            footer = (" ↑/↓ or j/k move   space toggle   e export   q quit"
                     f"   {status}")
            try:
                stdscr.addnstr(h - 1, 0, footer[:w - 1], max(0, w - 1), teal_attr)
            except curses.error:
                pass
            stdscr.refresh()

            key = stdscr.getch()
            if key in (ord("q"), 27):
                return
            if key in (ord("j"), curses.KEY_DOWN):
                if checkbox_idx:
                    cursor = min(cursor + 1, len(checkbox_idx) - 1)
                else:
                    top = min(top + 1, max(0, len(lines) - body_h))
            elif key in (ord("k"), curses.KEY_UP):
                if checkbox_idx:
                    cursor = max(cursor - 1, 0)
                else:
                    top = max(top - 1, 0)
            elif key in (ord(" "), 10, 13, curses.KEY_ENTER):
                if cur_line >= 0:
                    lines[cur_line]["checked"] = not lines[cur_line]["checked"]
            elif key == ord("e"):
                try:
                    with open(export_path, "w") as f:
                        f.write("\n".join(_render_report_line(l) for l in lines) + "\n")
                    status = f"[saved {export_path}]"
                except OSError as e:
                    status = f"[save failed: {e}]"

    curses.wrapper(loop)
    return True


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


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="QA ASSIST — PR QA Test Planner")
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
    ap.add_argument("--skip-setup", action="store_true",
                    help="skip the automatic gh CLI install/auth check")
    ap.add_argument("--interactive", action="store_true",
                    help="launch the QA ASSIST interactive menu")
    ap.add_argument("--plain", action="store_true",
                    help="print flat markdown instead of the interactive "
                         "checklist viewer (for logs/piping/tmux capture)")
    ap.add_argument("--review-check", action="store_true",
                    help="check review-submission quality instead of planning")
    ap.add_argument("--sweep", action="store_true",
                    help="(with --review-check) scan recent merged PRs, not one")
    ap.add_argument("--all-repos", action="store_true",
                    help="(with --sweep) scan every repo the QA team reviews")
    ap.add_argument("--limit", type=int, default=30,
                    help="(with --sweep) PRs per repo to scan (default: 30)")
    ap.add_argument("--nudge", action="store_true",
                    help="(with --review-check) post a nudge comment if the "
                         "review is incomplete")
    return ap


def run_plan(args) -> None:
    if not args.repo:
        with Spinner("Detecting current repo..."):
            r = gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        if r.returncode != 0:
            raise SystemExit("Could not detect repo; pass --repo OWNER/REPO")
        args.repo = r.stdout.strip()

    pr_number = resolve_pr(args.repo, args.pr)
    with Spinner(f"Fetching {args.repo}#{pr_number}..."):
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

    with Spinner("Building service map (dtm / .gitmodules)..."):
        service_map = build_service_map(args.org)
    plan = analyze(files, service_map)
    plan["pr_url"] = pr.get("html_url")
    plan["repo"] = args.repo
    with Spinner("Checking cross-PR references and submodule bumps..."):
        plan["referenced_prs"] = find_referenced_prs(args.repo, pr.get("body") or "",
                                                     pr.get("number", 0))
        plan["submodule_bump_details"] = resolve_submodule_bumps(files, args.org)
    if args.deep:
        with Spinner("Deep-scanning local checkout..."):
            plan["deep"] = deep_analyze(args.repo, pr, files, args.workspace)

    if args.json:
        print(json.dumps(plan, indent=2))
        return

    md = render_markdown(plan, pr, args.repo)

    if args.out:
        with open(args.out, "w") as f:
            f.write(md + "\n")
        print(f"[written] {args.out}", file=sys.stderr)

    shown_interactively = False
    if not args.plain and sys.stdout.isatty():
        default_export = args.out or f"qa-plan-{args.repo.split('/')[-1]}-{pr_number}.md"
        shown_interactively = run_interactive_report(md, default_export)
    if not shown_interactively:
        print(md)

    if args.comment:
        body = md + "\n\n---\n_Comment generated by `deepiri-suite/scripts/pr-qa-planner.py`._"
        with Spinner("Posting comment..."):
            c = gh("pr", "comment", "--repo", args.repo, str(pr_number), "--body", body)
        if c.returncode != 0:
            raise SystemExit(f"Failed to post comment: {c.stderr.strip()}")
        print(f"\n[posted] QA plan as comment on {pr.get('html_url')}", file=sys.stderr)


def run_interactive(base_args) -> None:
    """QA ASSIST menu — for anyone who'd rather answer prompts than learn flags."""
    print_banner()
    menu = [
        ("Generate a QA test plan for a PR", "plan"),
        ("Deep-scan a PR (find exact tests to run)", "deep"),
        ("Check one PR's review quality", "review"),
        ("Sweep review-quality compliance across repos", "sweep"),
        ("Exit", "exit"),
    ]
    while True:
        print(f"\n{BOLD}{BLUE}QA ASSIST{RESET} — what would you like to do?")
        for i, (label, _) in enumerate(menu, 1):
            print(f"  {TEAL}{i}{RESET}) {label}")
        try:
            choice = input(f"{TEAL}?{RESET} Choice [1-{len(menu)}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not choice.isdigit() or not (1 <= int(choice) <= len(menu)):
            print("Please enter a number from the menu.")
            continue
        action = menu[int(choice) - 1][1]
        if action == "exit":
            return

        args = argparse.Namespace(**vars(base_args))
        if action in ("plan", "deep", "review"):
            args.repo = input(f"{TEAL}?{RESET} Repo (e.g. Team-Deepiri/deepiri-auth-service): ").strip() or None
            pr_in = input(f"{TEAL}?{RESET} PR number (blank = current branch's PR): ").strip()
            args.pr = int(pr_in) if pr_in else None

        if action == "plan":
            args.deep = False
            run_plan(args)
        elif action == "deep":
            args.deep = True
            run_plan(args)
        elif action == "review":
            args.review_check = True
            args.sweep = False
            args.all_repos = False
            nudge_in = input(f"{TEAL}?{RESET} Post a nudge comment if incomplete? [y/N]: ").strip().lower()
            args.nudge = nudge_in in ("y", "yes")
            run_review_check(args)
        elif action == "sweep":
            args.review_check = True
            args.sweep = True
            all_in = input(f"{TEAL}?{RESET} Sweep every repo the QA team reviews? [y/N]: ").strip().lower()
            args.all_repos = all_in in ("y", "yes")
            if not args.all_repos:
                args.repo = input(f"{TEAL}?{RESET} Repo to sweep: ").strip() or None
            limit_in = input(f"{TEAL}?{RESET} PRs per repo [30]: ").strip()
            args.limit = int(limit_in) if limit_in else 30
            run_review_check(args)


def main():
    args = build_arg_parser().parse_args()

    if not args.json:
        print_banner()

    ensure_gh_ready(skip=args.skip_setup)
    maybe_install_opencode(skip=args.skip_setup)

    no_action = not args.repo and not args.pr and not args.review_check
    if args.interactive or (no_action and sys.stdin.isatty() and sys.stdout.isatty()):
        run_interactive(args)
        return

    if args.review_check:
        run_review_check(args)
        return

    run_plan(args)


if __name__ == "__main__":
    main()
