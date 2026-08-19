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
import glob
import http
import itertools
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import io
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile
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


# ---------------------------------------------------------------------------
# Better than guessing package managers: for a tool that publishes official
# prebuilt binaries on GitHub Releases, download the right one directly —
# no apt/dnf/pacman/brew/winget knowledge needed at all, works identically
# on any OS/distro, no sudo required (installs into the user's own bin dir).
# This is the primary install path; the package-manager table below is only
# a fallback for when this can't run (offline, rate-limited, no matching
# release asset).
# ---------------------------------------------------------------------------

def _os_arch_tags() -> tuple[list[str], list[str]]:
    """Tokens likely to appear in a release asset name for this OS/CPU —
    derived live from platform.system()/platform.machine(), not a fixed
    per-tool table."""
    system = platform.system().lower()
    os_tags = {
        "linux": ["linux"],
        "darwin": ["darwin", "macos", "apple"],
        "windows": ["windows", "win"],
    }.get(system, [system])
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch_tags = ["x86_64", "amd64", "x64"]
    elif machine in ("arm64", "aarch64"):
        arch_tags = ["arm64", "aarch64"]
    else:
        arch_tags = [machine]
    return os_tags, arch_tags


def _pick_release_asset(assets: list[dict]) -> dict | None:
    os_tags, arch_tags = _os_arch_tags()
    candidates = [a for a in assets
                 if any(t in a["name"].lower() for t in os_tags)
                 and any(t in a["name"].lower() for t in arch_tags)
                 and a["name"].lower().endswith((".tar.gz", ".tgz", ".zip"))]
    # musl/static builds are the safest bet on an unknown Linux distro
    # (no glibc-version assumptions) — prefer them when present.
    if platform.system().lower() == "linux":
        musl = [a for a in candidates if "musl" in a["name"].lower()]
        if musl:
            return musl[0]
    return candidates[0] if candidates else None


def install_from_github_release(gh_repo: str, binary_name: str, dest_dir: str | None = None) -> bool:
    """Fetch the latest release of `gh_repo` and install whichever binary
    inside matches `binary_name` for this OS/arch. Uses urllib directly
    (not the `gh` CLI) since this may be called to install `gh` itself."""
    dest_dir = dest_dir or os.path.join(os.path.expanduser("~"), ".local", "bin")
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{gh_repo}/releases/latest",
            headers={"User-Agent": "pr-qa-planner", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            release = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return False
    asset = _pick_release_asset(release.get("assets", []))
    if not asset:
        return False
    try:
        req = urllib.request.Request(asset["browser_download_url"],
                                     headers={"User-Agent": "pr-qa-planner"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            blob = resp.read()
    except (urllib.error.URLError, OSError, TimeoutError):
        return False

    target_name = binary_name + (".exe" if platform.system() == "Windows" else "")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            if asset["name"].endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                    zf.extractall(tmp)
            else:
                with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
                    tf.extractall(tmp)
        except (zipfile.BadZipFile, tarfile.TarError, OSError):
            return False
        found = None
        for dirpath, _dirs, filenames in os.walk(tmp):
            if target_name in filenames:
                found = os.path.join(dirpath, target_name)
                break
        if not found:
            return False
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, target_name)
        try:
            shutil.copy2(found, dest_path)
            os.chmod(dest_path, 0o755)
        except OSError:
            return False
    if dest_dir not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"[setup] installed to {dest_path} — add {dest_dir} to your PATH "
              "if this isn't picked up automatically.", file=sys.stderr)
    return shutil.which(target_name) is not None or os.path.isfile(dest_path)


# One generic package-manager table shared by every auto-installer in this
# script, instead of the same if/elif "apt-get, dnf, pacman, snap, brew,
# winget" chain hand-copied per tool (and each copy only covering a subset).
# Note WSL needs no special-casing here: it runs a real Linux distro, so its
# distro's normal manager (apt/dnf/pacman/zypper/...) already applies —
# `platform.system()` correctly reports "Linux" inside WSL.
PACKAGE_MANAGERS: list[tuple[str, list[str]]] = [
    ("brew", ["brew", "install", "{pkg}"]),
    ("apt-get", ["sudo", "apt-get", "install", "-y", "{pkg}"]),
    ("dnf", ["sudo", "dnf", "install", "-y", "{pkg}"]),
    ("yum", ["sudo", "yum", "install", "-y", "{pkg}"]),
    ("pacman", ["sudo", "pacman", "-S", "--noconfirm", "{pkg}"]),
    ("zypper", ["sudo", "zypper", "install", "-y", "{pkg}"]),
    ("apk", ["sudo", "apk", "add", "{pkg}"]),
    ("snap", ["sudo", "snap", "install", "{pkg}"]),
    ("winget", ["winget", "install", "--id", "{pkg}"]),
    ("choco", ["choco", "install", "-y", "{pkg}"]),
    ("scoop", ["scoop", "install", "{pkg}"]),
]


def install_via_package_manager(pkg_names: dict[str, str], timeout: int = 180) -> bool:
    """Try every package manager actually present on this machine, in the
    order above, until one succeeds. `pkg_names` maps manager-binary -> the
    package name for that manager (names sometimes differ, e.g. "ripgrep"
    vs winget's "BurntSushi.ripgrep.MSVC") — a manager not present in
    `pkg_names` is skipped even if it's installed, so callers only offer
    managers they actually have a package name for."""
    for binary, template in PACKAGE_MANAGERS:
        pkg = pkg_names.get(binary)
        if not pkg or not shutil.which(binary):
            continue
        cmd = [part.format(pkg=pkg) for part in template]
        try:
            if subprocess.run(cmd, timeout=timeout).returncode == 0:
                return True
        except (subprocess.TimeoutExpired, OSError):
            continue
    return False


def _install_gh() -> bool:
    """Install the gh CLI. Tries the official prebuilt binary release first
    (no package-manager guessing needed at all), then falls back to
    whichever package manager this machine actually has."""
    if install_from_github_release("cli/cli", "gh"):
        return True
    if install_via_package_manager({
        "brew": "gh", "apt-get": "gh", "dnf": "gh", "yum": "gh",
        "pacman": "github-cli", "zypper": "gh", "apk": "github-cli",
        "snap": "gh", "winget": "GitHub.cli", "choco": "gh", "scoop": "gh",
    }):
        return True
    # apt's default repos don't carry `gh` on some releases — add the
    # official apt source (per https://cli.github.com/) and retry.
    if shutil.which("apt-get"):
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
        try:
            return subprocess.run(["bash", "-c", setup], timeout=180).returncode == 0
        except (subprocess.TimeoutExpired, OSError):
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
    try:
        r = subprocess.run(["dtm", "scan", "--path", platform_root],
                           capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print("[warn] dtm scan timed out after 120s — falling back to .gitmodules",
              file=sys.stderr)
        return None
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


def fetch_gitmodules_text(repo: str) -> str | None:
    """Fetch a repo's own .gitmodules via the GitHub API — works with no
    local checkout, still a live read instead of a hardcoded list."""
    r = gh("api", f"repos/{repo}/contents/.gitmodules", "--jq", ".content")
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


def guess_test_command_from_changed_paths(paths: list[str]) -> str:
    """Guess a test command from the PR's own changed-file extensions —
    more accurate than a repo-wide GitHub `language` field, which reflects
    total bytes across the whole repo (misleading for a mixed-language repo
    where this specific PR only touches one side of it)."""
    exts = {os.path.splitext(p)[1].lower() for p in paths}
    if exts & {".ts", ".tsx", ".js", ".jsx"}:
        return "npm test"
    if ".py" in exts:
        return "pytest"
    if ".go" in exts:
        return "go test ./..."
    return "manual"


def build_service_map_from_gitmodules(repo: str) -> list[tuple[str, str, str]]:
    """Fallback when dtm isn't available/installable: parse the analyzed
    repo's own .gitmodules for path->repo, then guess the test command from
    each submodule's GitHub language. Still fully dynamic — no per-repo
    list to maintain by hand, and no assumption about which repo in the org
    is "the monorepo": whichever repo is being analyzed either has its own
    .gitmodules (and this returns real entries) or it doesn't (empty list,
    and analyze() treats it as a standalone repo)."""
    text = fetch_gitmodules_text(repo)
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
        # Submodules live in the same org as their parent monorepo — no need
        # to parse the org out of the .gitmodules URL (SSH-style
        # git@host:org/repo.git doesn't split cleanly on "/" anyway).
        cmd = guess_test_command_via_language(f"{repo.split('/')[0]}/{short}")
        entries.append((path, label, f"cd {path} && {cmd}"))
    return entries


def build_service_map(repo: str, org: str) -> list[tuple[str, str, str]]:
    """The live path->(area, test command) map used by classify_path, built
    fresh each run from the repo actually being analyzed — not a fixed
    "the monorepo is always named X" assumption. Tries a local dtm scan of
    that repo first (precise), falls back to that repo's own .gitmodules
    (works anywhere, no local checkout needed). Both naturally return an
    empty list for a repo with no submodule structure of its own — analyze()
    treats that as "this PR's whole repo is its own area" rather than trying
    to force a path-prefix match that can't exist."""
    local_root = repo_root_hint(repo)
    if local_root:
        from_dtm = build_service_map_from_dtm(local_root)
        if from_dtm:
            return from_dtm
    return build_service_map_from_gitmodules(repo)

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
OLD_SUBPROJECT_SHA_RE = re.compile(r"^-Subproject commit ([0-9a-f]{40})", re.MULTILINE)

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
            repo: str | None = None, changed_files_before: int = 0) -> dict:
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

    # service_map is now built from the analyzed repo's OWN submodule
    # structure (see build_service_map): it's non-empty exactly when this
    # repo has submodules of its own (a monorepo, whatever it's named), and
    # empty when it doesn't (a standalone repo, whose paths are repo-
    # relative like "src/App.tsx" and could never match a path prefix
    # anyway). So "no service_map at all" — not a hardcoded repo name — is
    # the correct, fully dynamic signal to treat the whole repo as one area
    # instead of dumping everything into "(unknown / other)".
    whole_repo_label, whole_repo_cmd = None, None
    if repo and not service_map:
        short = repo.split("/")[-1]
        whole_repo_label = prettify_service_name(short)
        whole_repo_cmd = guess_test_command_from_changed_paths(all_paths)

    for f in files:
        label, cmd = classify_path(f["filename"], service_map)
        if not label and whole_repo_label:
            label, cmd = whole_repo_label, whole_repo_cmd
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

# The HTTP verbs this script knows how to spot in route decorators/calls —
# sourced from Python's own stdlib http.HTTPMethod enum (the language's
# canonical definition of what an HTTP method is) rather than typed out by
# hand, and defined once here instead of copy-pasted into four regexes.
# CONNECT/TRACE are excluded — no web framework registers routes for them.
HTTP_METHODS = tuple(m.value.lower() for m in http.HTTPMethod
                     if m not in (http.HTTPMethod.CONNECT, http.HTTPMethod.TRACE))
_METHOD_ALT = "|".join(HTTP_METHODS)

PY_SYMBOL = re.compile(
    r"^(?:async\s+def|def)\s+(\w+)"
    r"|^class\s+(\w+)"
    rf"|@\w+\.(?:{_METHOD_ALT})\(\s*[\"']([^\"']*)",
    re.MULTILINE,
)
# Order matters: the more specific function/class alternatives must come
# before the bare "export default <name>" fallback, or `export default
# function Home()` gets misread as a symbol literally named "function" (the
# generic alt's \w+ greedily grabs the "function" keyword itself).
TS_SYMBOL = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)"
    r"|^(?:export\s+)?(?:default\s+)?class\s+(\w+)"
    r"|^(?:export\s+)?const\s+(\w+)\s*="
    r"|^export\s+default\s+(\w+)\b"
    rf"|\.(?:{_METHOD_ALT})\(\s*[\"']([^\"']*)",
    re.MULTILINE,
)
IMPORT_RE = re.compile(r"(?:from\s+|require\(|import\s*\(?)[\"']([^\"']+)[\"']")

# Route decorators/calls with method captured separately from the symbol
# regexes above — needed to generate a real curl command per route instead
# of just listing "a route changed".
PY_ROUTE = re.compile(rf"@\w+\.({_METHOD_ALT})\(\s*[\"']([^\"']*)",
                     re.MULTILINE | re.IGNORECASE)
TS_ROUTE = re.compile(rf"\.({_METHOD_ALT})\(\s*[\"']([^\"']*)",
                     re.MULTILINE | re.IGNORECASE)

# A file counts as "frontend" for visual-observation purposes by its own
# extension/path shape, not by which repo it's in.
FRONTEND_PATH_HINTS = (".tsx", ".jsx", "/components/", "/pages/", "/views/", "/screens/")


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


def extract_routes(content: str, path: str) -> list[tuple[str, str]]:
    """(method, route_path) pairs actually defined in this file — real data
    from the diff, not a guess — so a curl command can be generated per one."""
    regex = PY_ROUTE if path.endswith(".py") else TS_ROUTE
    return sorted(set((m.upper(), p) for m, p in regex.findall(content)))


def is_frontend_path(path: str) -> bool:
    low = path.lower()
    return any(hint in low for hint in FRONTEND_PATH_HINTS)


def find_compose_files(directory: str) -> list[str]:
    """Docker Compose files in a directory, discovered by Compose's own
    naming convention (`*compose*.y*ml` — covers docker-compose.yml,
    docker-compose.dev.yml, compose.yaml, compose.override.yml, etc.)
    instead of a fixed hardcoded filename list. A "dev" file sorts first
    since that's the one local QA actually runs against."""
    matches = [p for p in glob.glob(os.path.join(directory, "*compose*.y*ml"))
              if os.path.isfile(p)]
    matches.sort(key=lambda p: (0 if "dev" in os.path.basename(p).lower() else 1,
                                len(os.path.basename(p))))
    return matches


def find_compose_root() -> str | None:
    """Find a local checkout with docker-compose files, without assuming
    which repo it is by name — starts from the current git top-level and
    walks UP through ancestor directories (this script is commonly run from
    inside a nested submodule, e.g. deepiri-suite/, whose own git top-level
    is itself, not the platform monorepo a few levels up)."""
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    candidates = [top]
    parent = os.path.dirname(top)
    for _ in range(6):
        if not parent or parent == os.path.dirname(parent):
            break
        candidates.append(parent)
        parent = os.path.dirname(parent)
    for c in candidates:
        if find_compose_files(c):
            return c
    return None


def find_service_info(compose_root: str, short_repo: str) -> dict | None:
    """Read a service's real port, container name, and compose service key
    straight out of the platform's own docker-compose files — no hardcoded
    port/container table. Matches on `container_name`, which this platform's
    compose files consistently set to `<repo-short-name>-dev`."""
    for compose_path in find_compose_files(compose_root):
        fname = os.path.basename(compose_path)
        try:
            with open(compose_path) as fh:
                text = fh.read()
        except OSError:
            continue
        m = (re.search(rf"container_name:\s*({re.escape(short_repo)}-dev)\b", text)
             or re.search(rf"container_name:\s*({re.escape(short_repo)})\b", text))
        if not m:
            continue
        container_name = m.group(1)
        before = text[:m.start()]
        key_matches = list(re.finditer(r"\n {2}([\w-]+):\n", before))
        service_key = key_matches[-1].group(1) if key_matches else short_repo
        rest = text[m.end():]
        next_service = re.search(r"\n {2}\S[^\n]*:\n", rest)
        block = rest[:next_service.start()] if next_service else rest
        port_m = re.search(r'-\s*"?(\d+):(\d+)"?', block)
        return {
            "service_key": service_key,
            "container_name": container_name,
            "port": port_m.group(1) if port_m else None,
            "compose_file": fname,
        }
    return None


_CONTAINER_ENGINE: str | None = None
_COMPOSE_CMD: str | None = None


# Ranked by how likely each is to be what's actually installed today. This
# list can't be exhaustive forever — whatever containerization tool exists
# in five years isn't in it — so QA_CONTAINER_ENGINE lets anyone point this
# at something new without needing a code change.
# Every container CLI that actually exists in the wild today. Not a
# preference/assumption — detect_container_engine() searches the device for
# whichever of these is actually installed rather than assuming any one of
# them; this list is just what "search the device" is searching *for*, the
# same way rg_files needs to know "rg" is the ripgrep binary's name.
KNOWN_CONTAINER_ENGINES = ("docker", "podman", "nerdctl", "finch")


def detect_container_engine() -> str:
    """Search the device for whichever known container CLI is actually
    installed, in the order above — no config, no assumption that Docker
    is the only option."""
    global _CONTAINER_ENGINE
    if _CONTAINER_ENGINE is not None:
        return _CONTAINER_ENGINE
    for engine in KNOWN_CONTAINER_ENGINES:
        if shutil.which(engine):
            _CONTAINER_ENGINE = engine
            return _CONTAINER_ENGINE
    _CONTAINER_ENGINE = "docker"  # none present — most common default guess
    return _CONTAINER_ENGINE


def detect_compose_command() -> str:
    """Compose ships multiple incompatible invocations depending on engine
    and install: the v2 plugin ('docker compose'/'podman compose', space)
    or a legacy standalone binary ('docker-compose'/'podman-compose',
    hyphen). Detect which this machine actually has instead of assuming
    one — and detect it for whichever engine is actually present."""
    global _COMPOSE_CMD
    if _COMPOSE_CMD is not None:
        return _COMPOSE_CMD
    engine = detect_container_engine()
    if shutil.which(engine):
        try:
            r = subprocess.run([engine, "compose", "version"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                _COMPOSE_CMD = f"{engine} compose"
                return _COMPOSE_CMD
        except (subprocess.TimeoutExpired, OSError):
            pass
    standalone = "podman-compose" if engine == "podman" else "docker-compose"
    if shutil.which(standalone):
        _COMPOSE_CMD = standalone
        return _COMPOSE_CMD
    _COMPOSE_CMD = f"{engine} compose"  # neither detectable — most common default
    return _COMPOSE_CMD


def build_docker_commands(service_info: dict | None) -> list[str]:
    """Standard debug commands for one service's container. `logs`/`exec`/
    `compose restart` are a container CLI's own fixed vocabulary (no
    "dynamic source" for a tool's own command names, same as `curl -X` or
    `git checkout`) — but WHICH engine (docker vs podman), which compose
    invocation, and the container's shell all genuinely vary by
    environment, so those are detected live rather than assumed."""
    if not service_info:
        return []
    engine = detect_container_engine()
    compose_cmd = detect_compose_command()
    container = service_info["container_name"]
    return [
        f"{engine} logs -f {container}",
        # bash if the image has it, falling back to sh (busybox/alpine
        # images usually don't ship bash) — tried live, not assumed.
        f"{engine} exec -it {container} bash || {engine} exec -it {container} sh",
        f"{compose_cmd} -f {service_info['compose_file']} restart {service_info['service_key']}",
    ]


def find_frontend_dev_port(repo_path: str) -> str | None:
    """Read the actual dev-server port straight out of the repo's own
    package.json/vite config — no hardcoded default like 3000."""
    pkg = os.path.join(repo_path, "package.json")
    if os.path.isfile(pkg):
        try:
            with open(pkg) as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            data = {}
        dev_script = (data.get("scripts", {}) or {}).get("dev", "")
        m = re.search(r"--port[= ](\d+)", dev_script)
        if m:
            return m.group(1)
    for cfg_name in ("vite.config.ts", "vite.config.js"):
        cfg = os.path.join(repo_path, cfg_name)
        if os.path.isfile(cfg):
            try:
                with open(cfg) as fh:
                    text = fh.read()
            except OSError:
                continue
            m = re.search(r"port:\s*(\d+)", text)
            if m:
                return m.group(1)
    return None


def route_to_curl(method: str, route_path: str, port: str | None) -> str:
    """A real, runnable curl command for one actually-changed route."""
    host = f"http://localhost:{port}" if port else "http://localhost:<PORT — not found in docker-compose, check the service's port mapping>"
    if method in ("POST", "PUT", "PATCH"):
        return f'curl -X {method} {host}{route_path} -H "Content-Type: application/json" -d \'{{}}\''
    return f"curl -X {method} {host}{route_path}"


def repo_root_hint(repo: str) -> str | None:
    """Return a local path for the PR's repo if we can find it cheaply.

    Checks, in order:
      1. current directory is inside a git repo whose remote matches `repo`
      2. that repo's ancestor directories, in case cwd is itself inside a
         nested submodule (e.g. running from deepiri-suite/, whose own git
         top-level is deepiri-suite itself, not the platform monorepo that
         contains it) — walks up looking for a `.gitmodules` referencing the
         target, exactly like check 1 but from higher up the tree
      3. DEEPIRI_QA_WORKSPACE env var (QA's checked-out clone root)
      4. sibling/child dirs named after the repo's short name
    """
    short = repo.split("/")[-1]
    candidates: list[str] = []

    def add_from_top(top: str) -> None:
        candidates.append(top)
        # If `top` is a monorepo, the target repo may be a submodule nested
        # inside it — resolve it via .gitmodules (cheap, exact).
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

    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        add_from_top(top)
        # Walk up a bounded number of ancestor directories in case `top`
        # is itself a submodule nested inside a larger monorepo checkout.
        parent = os.path.dirname(top)
        for _ in range(6):
            if not parent or parent == os.path.dirname(parent):
                break
            if os.path.isdir(os.path.join(parent, ".git")):
                add_from_top(parent)
            parent = os.path.dirname(parent)
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


_RG_READY: bool | None = None


def ensure_rg_ready() -> bool:
    """--deep's rg_files() silently returned nothing on a machine without
    ripgrep — not a crash, just quietly worse results. Auto-install it like
    gh/dtm so --deep actually works out of the box. Cached per run."""
    global _RG_READY
    if _RG_READY is not None:
        return _RG_READY
    if shutil.which("rg"):
        _RG_READY = True
        return True
    print("[setup] ripgrep ('rg') not found — attempting to install it...",
          file=sys.stderr)
    ok = install_from_github_release("BurntSushi/ripgrep", "rg") or install_via_package_manager({
        "brew": "ripgrep", "apt-get": "ripgrep", "dnf": "ripgrep", "yum": "ripgrep",
        "pacman": "ripgrep", "zypper": "ripgrep", "apk": "ripgrep", "snap": "ripgrep",
        "winget": "BurntSushi.ripgrep.MSVC", "choco": "ripgrep", "scoop": "ripgrep",
    })
    if not ok or shutil.which("rg") is None:
        print("[warn] could not auto-install ripgrep — --deep's cross-reference "
              "search will find nothing. Install manually: https://github.com/BurntSushi/ripgrep",
              file=sys.stderr)
        _RG_READY = False
        return False
    _RG_READY = True
    return True


def rg_files(repo_path: str, pattern: str, extra_globs: list[str] | None = None) -> list[str]:
    """Files containing `pattern`, via ripgrep (gitignored dirs auto-skipped)."""
    if not ensure_rg_ready():
        return []
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


def diff_files_between(repo_path: str, old_rev: str, new_rev: str) -> list[str]:
    try:
        r = subprocess.run(["git", "-C", repo_path, "diff", "--name-only", old_rev, new_rev],
                           capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return []
    if r.returncode != 0:
        return []
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def _scan_one_file(root: str, rev: str, path: str, cd_path: str, js_runner: str | None,
                    backend_port: str | None, frontend_port_state: list[str]) -> dict | None:
    """The per-file deep-scan logic, shared between the PR's own changed
    files and files changed inside a bumped submodule's diff.
    `frontend_port_state` is a shared 0/1-item list used as a lazily-filled
    cache slot for the frontend dev port across repeated calls."""
    if not path.endswith((".py", ".ts", ".tsx", ".js", ".jsx")):
        return None
    content = git_show_file(root, rev, path)
    if content is None:
        return None
    syms = extract_symbols(content, path)
    imports = extract_imports(content)
    routes = extract_routes(content, path)
    route_commands = [(m, p, route_to_curl(m, p, backend_port)) for m, p in routes]

    visual_checks = []
    if is_frontend_path(path):
        if not frontend_port_state:
            frontend_port_state.append(find_frontend_dev_port(root) or "")
        frontend_port = frontend_port_state[0]
        named_syms = [s for s in syms if s not in GENERIC_SYMBOLS]
        if named_syms:
            dev_url = (f"http://localhost:{frontend_port}" if frontend_port
                      else "the dev server (port not found in package.json/vite.config - check how this repo's frontend starts)")
            for s in named_syms:
                visual_checks.append(
                    f"Open {dev_url} and visually verify `{s}` (changed in `{path}`) - "
                    "check it renders, layout/spacing look right, no console errors."
                )
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
    users = sorted(set(os.path.relpath(u, root) for u in users
                       if u.split("/")[-1] != base))[:12]
    neighbors = sorted(set(os.path.relpath(n, root) for n in neighbors))[:8]
    test_files = sorted(set(u for u in users if any(m in u for m in TEST_MARKERS)))[:6]
    return {
        "path": path,
        "content": content,
        "symbols": syms,
        "imports": imports,
        "neighbors": neighbors,
        "users": users,
        "tests": test_files,
        "test_commands": [test_command_for(cd_path, t, js_runner) for t in test_files],
        "routes": route_commands,
        "visual_checks": visual_checks,
    }


def fetch_repo_file_remote(repo: str, ref: str, path: str) -> str | None:
    """A file's content straight from the GitHub API at a specific commit —
    no local clone needed. Used when the analyzed repo has no local
    checkout, so --deep still produces real commands instead of just
    telling the user to go clone something first."""
    r = gh("api", f"repos/{repo}/contents/{path}?ref={ref}", "--jq", ".content")
    if r.returncode != 0 or not r.stdout.strip():
        return None
    import base64
    try:
        return base64.b64decode(r.stdout.strip()).decode("utf-8", errors="replace")
    except (ValueError, UnicodeDecodeError):
        return None


def resolve_relative_import(base_path: str, imp: str, known_extensions: set[str] | None = None) -> list[str]:
    """Candidate resolved paths for a relative import spec, trying
    extensions and index files — pure path math, no filesystem/clone
    needed, so this works against a remote-only fetch.

    `known_extensions` should be the extensions actually observed among the
    PR's own changed files — deriving candidates from what the project is
    actually written in, instead of a fixed hardcoded language list that
    would silently miss anything outside it (Go, Ruby, Vue SFCs, whatever)."""
    if not imp.startswith("."):
        return []
    joined = os.path.normpath(os.path.join(os.path.dirname(base_path), imp)).replace("\\", "/")
    if os.path.splitext(joined)[1]:
        return [joined]
    exts = known_extensions or {".ts", ".tsx", ".js", ".jsx", ".py"}  # fallback only if nothing else is known
    js_like = {e for e in exts if e in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")}
    candidates = [joined + ext for ext in exts]
    candidates += [f"{joined}/index{ext}" for ext in (js_like or exts)]
    return candidates


def deep_analyze_remote(repo: str, files: list[dict], head_sha: str) -> dict:
    """Deep scan without any local checkout — fetches each changed file's
    content via the GitHub API instead. Gets routes/symbols/visual-checks
    (real commands you can actually run) but not the rg-based cross-
    reference search (who-else-references-this needs a full local tree to
    grep, which a single-file remote fetch can't provide)."""
    compose_root = find_compose_root()
    short = repo.split("/")[-1]
    service_info = find_service_info(compose_root, short) if compose_root else None
    backend_port = service_info["port"] if service_info else None
    docker_commands = build_docker_commands(service_info)

    frontend_port_state: list[str] = []
    pkg_json_cache: dict[str, str | None] = {}

    def remote_frontend_port() -> str | None:
        if "content" not in pkg_json_cache:
            pkg_json_cache["content"] = fetch_repo_file_remote(repo, head_sha, "package.json")
        content = pkg_json_cache["content"]
        if not content:
            return None
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return None
        dev_script = (data.get("scripts", {}) or {}).get("dev", "")
        m = re.search(r"--port[= ](\d+)", dev_script)
        return m.group(1)

    changed = []
    for f in files:
        path = f["filename"]
        if not path.endswith((".py", ".ts", ".tsx", ".js", ".jsx")):
            continue
        content = fetch_repo_file_remote(repo, head_sha, path)
        if content is None:
            continue
        syms = extract_symbols(content, path)
        routes = extract_routes(content, path)
        route_commands = [(m, p, route_to_curl(m, p, backend_port)) for m, p in routes]
        visual_checks = []
        if is_frontend_path(path):
            if not frontend_port_state:
                frontend_port_state.append(remote_frontend_port() or "")
            frontend_port = frontend_port_state[0]
            named_syms = [s for s in syms if s not in GENERIC_SYMBOLS]
            if named_syms:
                dev_url = (f"http://localhost:{frontend_port}" if frontend_port
                          else "the dev server (port not found remotely - check this repo's own dev-server setup)")
                for s in named_syms:
                    visual_checks.append(
                        f"Open {dev_url} and visually verify `{s}` (changed in `{path}`) - "
                        "check it renders, layout/spacing look right, no console errors."
                    )
        imports = extract_imports(content)
        changed.append({
            "path": path, "content": content, "symbols": syms, "imports": imports,
            "neighbors": [], "users": [], "tests": [], "test_commands": [],
            "routes": route_commands, "visual_checks": visual_checks,
        })

    # No local tree to grep for "who else references this" remotely, so the
    # semantic signal here is different: resolve each changed file's own
    # relative imports and fetch whichever candidate actually exists —
    # "this file needs these to make sense" instead of "these need this".
    changed_paths = {f["filename"] for f in files}
    known_extensions = {os.path.splitext(p)[1] for p in changed_paths if os.path.splitext(p)[1]}
    scores: dict[str, int] = {}
    for entry in changed:
        for imp in entry["imports"]:
            for candidate in resolve_relative_import(entry["path"], imp, known_extensions):
                if candidate in changed_paths:
                    continue
                scores[candidate] = scores.get(candidate, 0) + 2

    context_files = []
    for candidate, score in sorted(scores.items(), key=lambda kv: -kv[1]):
        content = fetch_repo_file_remote(repo, head_sha, candidate)
        if content is None:
            continue  # not every candidate extension actually exists
        if len(content) > MAX_CONTEXT_FILE_BYTES:
            content = content[:MAX_CONTEXT_FILE_BYTES] + \
                f"\n... [truncated — file is over {MAX_CONTEXT_FILE_BYTES // 1000}KB]"
        context_files.append({"path": candidate, "content": content, "score": score})
        if len(context_files) >= MAX_CONTEXT_FILES:
            break

    return {"available": True, "repo_path": f"{repo} (remote — no local checkout, fetched via GitHub API)",
            "head_sha": head_sha, "files": changed, "docker_commands": docker_commands,
            "remote_only": True, "context_files": context_files}


MAX_CONTEXT_FILES = 25
MAX_CONTEXT_FILE_BYTES = 50_000  # per-file cap so one huge generated/minified
                                 # file doesn't blow out the whole bundle


class ContextCollector:
    """Picks up to MAX_CONTEXT_FILES *unchanged but related* files to bundle
    full content for, alongside the PR's own changed files — the "what else
    does an engineer (or an AI handed this plan) need open to understand
    this change" set. Scored, not just "first N found":

      +3  it's a test file that exercises a changed file
      +2  it directly references a changed symbol ("users" from the rg hop)
      +2  the changed file directly imports it ("neighbors")
      +1  it sits in the same directory as the changed file
      +1  its filename stem overlaps the changed file's stem (Foo.ts /
          Foo.types.ts / useFoo.ts style companion files)

    Scores accumulate across every changed file that references a given
    candidate, so a shared util genuinely used everywhere naturally rises
    to the top instead of an arbitrary first-seen file."""

    def __init__(self):
        self._scores: dict[tuple[str, str, str], int] = {}

    def bump(self, root: str, rev: str, relpath: str, changed_path: str, base_score: int) -> None:
        score = base_score
        if os.path.dirname(relpath) == os.path.dirname(changed_path):
            score += 1
        changed_stem = os.path.splitext(os.path.basename(changed_path))[0].lower()
        path_stem = os.path.splitext(os.path.basename(relpath))[0].lower()
        if changed_stem and (changed_stem in path_stem or path_stem in changed_stem):
            score += 1
        key = (root, rev, relpath)
        self._scores[key] = self._scores.get(key, 0) + score

    def add_from_entry(self, root: str, rev: str, entry: dict, changed_path: str) -> None:
        for t in entry.get("tests", []):
            self.bump(root, rev, t, changed_path, 3)
        for u in entry.get("users", []):
            self.bump(root, rev, u, changed_path, 2)
        for n in entry.get("neighbors", []):
            self.bump(root, rev, n, changed_path, 2)

    def finalize(self, exclude: set[tuple[str, str]]) -> list[dict]:
        """Fetch content for the top-scored candidates, skipping anything
        already covered by the PR's own changed-file list."""
        ranked = sorted(self._scores.items(), key=lambda kv: -kv[1])
        out = []
        for (root, rev, relpath), score in ranked:
            if (root, relpath) in exclude:
                continue
            content = git_show_file(root, rev, relpath)
            if content is None:
                continue
            if len(content) > MAX_CONTEXT_FILE_BYTES:
                content = content[:MAX_CONTEXT_FILE_BYTES] + \
                    f"\n... [truncated — file is over {MAX_CONTEXT_FILE_BYTES // 1000}KB]"
            out.append({"path": relpath, "content": content, "score": score})
            if len(out) >= MAX_CONTEXT_FILES:
                break
        return out


def deep_analyze(repo: str, pr: dict, files: list[dict], workspace: str | None) -> dict:
    """Scan the local repo for what specifically exercises the changed code."""
    short = repo.split("/")[-1]
    root = repo_root_hint(repo)
    head_sha = pr.get("head", {}).get("sha")
    if head_sha is None:
        return {"available": False, "reason": "no_head_sha"}
    if not root:
        return deep_analyze_remote(repo, files, head_sha)

    if git_rev(root, head_sha) is None:
        r = subprocess.run(["git", "-C", root, "fetch", "origin",
                            pr.get("head", {}).get("ref", "")],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return {"available": False, "reason": "fetch_failed",
                    "clone_hint": f"git fetch origin {pr.get('head', {}).get('ref', '')}"}

    cd_path = os.path.relpath(root, os.getcwd()) or "."
    js_runner = detect_js_test_runner(root)
    # Backend service info (port, container, compose service key): read live
    # from whichever local checkout actually has docker-compose files — the
    # repo being analyzed itself (if it's the monorepo) or the checkout this
    # script is being run from/inside (if analyzing one of its submodules
    # directly). No hardcoded assumption about a specific repo's name.
    compose_root = root if find_compose_files(root) else find_compose_root()
    service_info = find_service_info(compose_root, short) if compose_root else None
    backend_port = service_info["port"] if service_info else None
    docker_commands = build_docker_commands(service_info)
    # Frontend dev-server port: read live from this repo's own package.json/
    # vite config — computed lazily, only if a frontend file actually changed.
    frontend_port_state: list[str] = []  # lazily-filled 0/1-item cache slot
    changed = []
    context = ContextCollector()
    for f in files:
        entry = _scan_one_file(root, head_sha, f["filename"], cd_path, js_runner,
                               backend_port, frontend_port_state)
        if entry:
            context.add_from_entry(root, head_sha, entry, entry["path"])
            changed.append(entry)

    # Submodule bumps show up here as a one-line gitlink pointer change, not
    # real file diffs — the code that actually changed lives inside the
    # bumped submodule's own commit range. If that submodule is checked out
    # locally, scan ITS diff the same way, so a platform PR that's mostly
    # submodule bumps still surfaces real curl/test/visual commands instead
    # of "nothing to scan".
    for f in files:
        if not is_gitlink_change(f):
            continue
        sub_path = f["filename"].rstrip("/")
        sub_short = sub_path.split("/")[-1]
        patch = f.get("patch") or ""
        new_m = SUBPROJECT_SHA_RE.search(patch)
        old_m = OLD_SUBPROJECT_SHA_RE.search(patch)
        if not new_m or not old_m:
            continue
        new_sha, old_sha = new_m.group(1), old_m.group(1)
        sub_root = repo_root_hint(f"{repo.split('/')[0]}/{sub_short}")
        if not sub_root:
            continue
        if git_rev(sub_root, new_sha) is None:
            subprocess.run(["git", "-C", sub_root, "fetch", "origin"],
                           capture_output=True, text=True, timeout=30)
        if git_rev(sub_root, new_sha) is None or git_rev(sub_root, old_sha) is None:
            continue  # can't diff without both revisions present locally
        sub_changed_paths = diff_files_between(sub_root, old_sha, new_sha)
        if not sub_changed_paths:
            continue

        sub_cd_path = os.path.relpath(sub_root, os.getcwd()) or "."
        sub_js_runner = detect_js_test_runner(sub_root)
        sub_service_info = find_service_info(compose_root, sub_short) if compose_root else None
        sub_backend_port = sub_service_info["port"] if sub_service_info else None
        for cmd_ in build_docker_commands(sub_service_info):
            if cmd_ not in docker_commands:
                docker_commands.append(cmd_)
        for sp in sub_changed_paths:
            entry = _scan_one_file(sub_root, new_sha, sp, sub_cd_path, sub_js_runner,
                                   sub_backend_port, frontend_port_state)
            if entry:
                context.add_from_entry(sub_root, new_sha, entry, sp)
                entry["path"] = f"{sub_path}/{sp}"
                changed.append(entry)

    exclude = {(root, f["filename"]) for f in files}
    context_files = context.finalize(exclude)

    return {"available": True, "repo_path": root, "head_sha": head_sha,
            "files": changed, "docker_commands": docker_commands,
            "context_files": context_files}


def render_deep(deep: dict) -> list[str]:
    lines = []
    if not deep.get("available"):
        lines.append("> **Deep scan unavailable:** couldn't resolve this PR's head commit.")
        return lines
    lines.append("#### What to test (deep scan)")
    lines.append("")
    if deep.get("remote_only"):
        lines.append(f"_Scanned `{deep.get('repo_path')}` at `{deep.get('head_sha', '')[:8]}` "
                     "— no local checkout found, so this fetched each changed file's content "
                     "directly via the GitHub API. Routes/visual-checks below are real; "
                     "cross-reference search (who else calls this, which test exercises it) "
                     "needs a local clone to grep across, so that part is skipped here._")
    else:
        lines.append(f"_Scanned `{deep.get('repo_path')}` at `{deep.get('head_sha', '')[:8]}` "
                     "— only changed files parsed, references found via ripgrep._")
    lines.append("")
    if deep.get("docker_commands"):
        lines.append("- **Service commands (docker):**")
        for cmd in deep["docker_commands"]:
            lines.append(f"  - `{cmd}`")
        lines.append("")
    if deep.get("context_files"):
        lines.append(f"- **{len(deep['context_files'])} related file(s) bundled for context** "
                     "(full content, not just names — ranked by relevance: test coverage, "
                     "references, shared imports, naming similarity). Not dumped here to keep "
                     "this readable; included automatically in the `p` AI-prompt export:")
        for cf in deep["context_files"]:
            lines.append(f"  - `{cf['path']}` (relevance {cf['score']})")
        lines.append("")
    any_hits = False
    for cf in deep.get("files", []):
        if not (cf["symbols"] or cf["neighbors"] or cf["users"] or cf["tests"]
                or cf.get("routes") or cf.get("visual_checks")):
            continue
        any_hits = True
        lines.append(f"- **`{cf['path']}`**")
        if cf["symbols"]:
            lines.append(f"  - Changed symbols/routes: `{', '.join(cf['symbols'])}`")
        if cf.get("routes"):
            lines.append("  - **Dynamic test commands — hit the changed route(s):**")
            for method, route_path, curl_cmd in cf["routes"]:
                lines.append(f"    - `{method} {route_path}`: `{curl_cmd}`")
        if cf["tests"]:
            lines.append("  - **Tests that exercise it — run these:**")
            for t, c in zip(cf["tests"], cf["test_commands"]):
                lines.append(f"    - `{t}`: `{c}`")
        if cf.get("visual_checks"):
            lines.append("  - **Visual observations to check:**")
            for vc in cf["visual_checks"]:
                lines.append(f"    - {vc}")
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


BACKTICK_RE = re.compile(r"`([^`]+)`")

CLIPBOARD_TOOLS = [
    ["xclip", "-selection", "clipboard"],
    ["xsel", "--clipboard", "--input"],
    ["wl-copy"],
    ["pbcopy"],
    ["clip.exe"],
]


def copy_to_clipboard(text: str) -> bool:
    for cmd in CLIPBOARD_TOOLS:
        if not shutil.which(cmd[0]):
            continue
        try:
            # xclip/xsel hang indefinitely if there's no working X display
            # (headless/SSH session, $DISPLAY unset) — a hard timeout keeps
            # a bad clipboard tool from freezing the whole interactive UI.
            r = subprocess.run(cmd, input=text, text=True, capture_output=True,
                               timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0:
            return True
    return False


def _copyable_text(raw: str) -> str:
    """The command in a line, if it looks like one — prefer the last
    backtick-fenced span (where curl/docker/test commands live), else the
    whole line."""
    codes = BACKTICK_RE.findall(raw)
    return codes[-1] if codes else raw.strip()


AI_AGENT_PROMPT_TEMPLATE = """You are acting as a QA engineer testing a pull request. Below is a complete QA test plan generated for it — environment setup, exact commands to run (curl/docker/test), visual checks, and a checklist — followed by the actual source of the changed files and related codebase context, so you're not working from the diff alone.

Work through the plan end to end: run every command shown and note its actual output/exit status, open any URLs mentioned to check the UI, and check off each item as you complete it. Use the source below to understand what the change actually does before judging whether a result is correct. If a command fails or a visual check looks wrong, say so specifically (what you expected vs. what happened) rather than just marking it failed. Finish with a summary: what passed, what failed, what needs a human to look at.

--- QA TEST PLAN ---
{plan_body}
--- END OF PLAN ---
{codebase_context}
Begin now."""


def _format_file_block(path: str, content: str, note: str = "") -> str:
    return f"\n### {path}{f' ({note})' if note else ''}\n```\n{content}\n```\n"


def synthesize_ai_prompt(lines: list[dict], deep: dict | None = None) -> str:
    """Package the current state of the report (including any checkboxes
    already toggled) into a single prompt an AI coding assistant can be
    handed to execute the whole test plan — plus the actual full content of
    every changed file and up to MAX_CONTEXT_FILES semantically-related
    unchanged files, not just the diff, so the assistant has real codebase
    context instead of an isolated patch."""
    plan_body = "\n".join(_render_report_line(l) for l in lines)
    codebase_context = ""
    if deep and deep.get("available"):
        blocks = []
        changed_files = deep.get("files", [])
        context_files = deep.get("context_files", [])
        if changed_files or context_files:
            blocks.append("\n--- CODEBASE CONTEXT ---")
        for cf in changed_files:
            if cf.get("content"):
                blocks.append(_format_file_block(cf["path"], cf["content"], "changed"))
        for cf in context_files:
            blocks.append(_format_file_block(cf["path"], cf["content"],
                                             f"related, relevance {cf['score']}"))
        if len(blocks) > 1:
            blocks.append("--- END CODEBASE CONTEXT ---\n")
            codebase_context = "\n".join(blocks)
    return AI_AGENT_PROMPT_TEMPLATE.format(plan_body=plan_body, codebase_context=codebase_context)


URL_RE = re.compile(r"https?://\S+")


def _extract_url(raw: str) -> str | None:
    m = URL_RE.search(raw)
    return m.group(0).rstrip(").,'\"") if m else None


def _run_shell_capture(cmd: str, timeout: int = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, f"[timed out after {timeout}s — command may still be running in the background]"
    except OSError as e:
        return -1, f"[failed to launch: {e}]"


def _show_output_pane(stdscr, curses, title: str, output: str, teal_attr) -> None:
    """Scrollable pane showing captured command output inline in the same
    terminal — this is the "visualize the output" ask: results appear right
    here, not in a separate window/terminal."""
    out_lines = output.splitlines() or ["(no output)"]
    top = 0
    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, f" {title}", max(0, w - 1), curses.A_BOLD)
        body_h = max(1, h - 2)
        for row in range(body_h):
            li = top + row
            if li >= len(out_lines):
                break
            try:
                stdscr.addnstr(row + 1, 0, out_lines[li], max(0, w - 1))
            except curses.error:
                pass
        footer = " up/down or j/k scroll   any other key: continue"
        try:
            stdscr.addnstr(h - 1, 0, footer[:w - 1], max(0, w - 1), teal_attr)
        except curses.error:
            pass
        stdscr.refresh()
        key = stdscr.getch()
        if key in (curses.KEY_DOWN, ord("j")):
            top = min(top + 1, max(0, len(out_lines) - body_h))
        elif key in (curses.KEY_UP, ord("k")):
            top = max(top - 1, 0)
        else:
            return


def _is_command_carrier(raw: str, kind: str) -> bool:
    """A line actually carries a runnable command — not just prose that
    happens to mention `something` in code font (e.g. "follows the
    `deepiri-qa-workflow` skill"). Checkbox lines with an embedded command
    (health-check/sorge-pass style) count; otherwise the line must itself be
    a bullet ("  - `cmd`" / "    - `label`: `cmd`"), not narrative text."""
    if not BACKTICK_RE.search(raw):
        return False
    if kind == "checkbox":
        return True
    return raw.strip().startswith("-")


def run_guided_walkthrough(stdscr, curses, lines: list[dict], teal_attr) -> None:
    """Step through every actionable line one at a time: run its command
    inline (output shown in the same terminal, not a separate window), open
    a browser tab for anything with a URL (visual checks), or just mark a
    checklist item done, then move on. This is the "walk me through testing
    this PR" ask."""
    actionable = [i for i, l in enumerate(lines)
                 if l["kind"] == "checkbox" or _is_command_carrier(l["raw"], l["kind"])
                 or URL_RE.search(l["raw"])]
    if not actionable:
        return
    idx = 0
    while 0 <= idx < len(actionable):
        li = actionable[idx]
        line = lines[li]
        raw = _render_report_line(line)
        cmd = _copyable_text(raw)
        is_runnable = (_is_command_carrier(raw, line["kind"])
                      and not cmd.lower().startswith(("http://", "https://")))
        url = _extract_url(raw)

        stdscr.erase()
        h, w = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, f" Guided walkthrough — step {idx + 1}/{len(actionable)}",
                       max(0, w - 1), curses.A_BOLD)
        for i, chunk_start in enumerate(range(0, max(len(raw), 1), max(1, w - 4))):
            if i + 2 >= h - 2:
                break
            stdscr.addnstr(i + 2, 2, raw[chunk_start:chunk_start + w - 4], max(0, w - 4))

        opts = []
        if line["kind"] == "checkbox":
            opts.append("space: mark done")
        if is_runnable:
            opts.append("r: run this command")
        if url:
            opts.append("o: open in browser")
        opts += ["n: next", "b: back", "q: exit walkthrough"]
        try:
            stdscr.addnstr(h - 1, 0, "   ".join(opts)[:w - 1], max(0, w - 1), teal_attr)
        except curses.error:
            pass
        stdscr.refresh()

        key = stdscr.getch()
        if key == ord("q"):
            return
        if key == ord("n"):
            idx += 1
        elif key == ord("b"):
            idx = max(0, idx - 1)
        elif key in (ord(" "), 10, 13, curses.KEY_ENTER) and line["kind"] == "checkbox":
            line["checked"] = not line["checked"]
        elif key == ord("r") and is_runnable:
            rc, output = _run_shell_capture(cmd)
            _show_output_pane(stdscr, curses, f"$ {cmd}   (exit {rc})", output, teal_attr)
        elif key == ord("o") and url:
            try:
                webbrowser.open(url)
            except Exception:
                pass


def _pick_export_path(stdscr, curses, start_dir: str, default_filename: str) -> str | None:
    """Inline mini file browser: navigate real directories from start_dir,
    Enter descends into a folder or (on the save entry) prompts for a
    filename in the current folder. Esc/q cancels."""
    cur_dir = os.path.abspath(start_dir)
    sel = 0
    while True:
        try:
            entries = os.listdir(cur_dir)
        except OSError:
            entries = []
        dirs = sorted(e for e in entries if os.path.isdir(os.path.join(cur_dir, e))
                     and not e.startswith("."))
        files = sorted(e for e in entries if not os.path.isdir(os.path.join(cur_dir, e))
                       and not e.startswith("."))
        rows = [f"[Save here as {default_filename}]", ".. (up one level)"]
        rows += [f"{d}/" for d in dirs] + files
        sel = max(0, min(sel, len(rows) - 1))

        stdscr.erase()
        h, w = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, f" Export to — current folder: {cur_dir}", max(0, w - 1),
                       curses.A_BOLD)
        for i, row in enumerate(rows):
            r = i + 2
            if r >= h - 1:
                break
            attr = curses.A_REVERSE if i == sel else curses.A_NORMAL
            stdscr.addnstr(r, 2, row, max(0, w - 3), attr)
        footer = " up/down or j/k move   Enter open/select   Esc/q cancel"
        try:
            stdscr.addnstr(h - 1, 0, footer[:w - 1], max(0, w - 1), curses.A_DIM)
        except curses.error:
            pass
        stdscr.refresh()

        key = stdscr.getch()
        if key in (ord("q"), 27):
            return None
        if key in (ord("j"), curses.KEY_DOWN):
            sel = min(sel + 1, len(rows) - 1)
        elif key in (ord("k"), curses.KEY_UP):
            sel = max(sel - 1, 0)
        elif key in (10, 13, curses.KEY_ENTER):
            choice = rows[sel]
            if choice.startswith("[Save here"):
                prompt = f" Filename [{default_filename}]: "
                stdscr.addnstr(h - 1, 0, prompt, max(0, w - 1))
                curses.echo()
                curses.curs_set(1)
                try:
                    raw = stdscr.getstr(h - 1, min(len(prompt), w - 1), 80)
                finally:
                    curses.curs_set(0)
                    curses.noecho()
                name = raw.decode("utf-8", errors="replace").strip() or default_filename
                return os.path.join(cur_dir, name)
            if choice == ".. (up one level)":
                cur_dir = os.path.dirname(cur_dir) or cur_dir
                sel = 0
            elif choice.endswith("/"):
                cur_dir = os.path.join(cur_dir, choice[:-1])
                sel = 0
            else:
                return os.path.join(cur_dir, choice)


def run_interactive_report(md: str, export_path: str, deep: dict | None = None) -> bool:
    """Curses checklist viewer over the generated plan. Returns True if it
    ran (caller shouldn't also flat-print); False if curses isn't usable
    here and the caller should fall back to a plain print. `deep` (if
    available) carries full changed-file + related-file content, bundled
    into the 'p' AI-prompt export so it's not just working from the diff."""
    try:
        import curses
    except ImportError:
        return False

    lines = _parse_report_lines(md)
    default_filename = os.path.basename(export_path)

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
        cursor = 0  # index into `lines` — moves across every line, not just checkboxes
        status = ""

        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            body_h = max(1, h - 2)
            if cursor < top:
                top = cursor
            elif cursor >= top + body_h:
                top = cursor - body_h + 1

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
                if li == cursor:
                    attr |= curses.A_REVERSE
                try:
                    stdscr.addnstr(row, 0, text, max(0, w - 1), attr)
                except curses.error:
                    pass

            footer = (" ↑/↓ or j/k move   space toggle   c copy   p AI prompt"
                     f"   w walkthrough   e export   q quit   {status}")
            try:
                stdscr.addnstr(h - 1, 0, footer[:w - 1], max(0, w - 1), teal_attr)
            except curses.error:
                pass
            stdscr.refresh()

            key = stdscr.getch()
            if key in (ord("q"), 27):
                return
            if key in (ord("j"), curses.KEY_DOWN):
                cursor = min(cursor + 1, len(lines) - 1)
                status = ""
            elif key in (ord("k"), curses.KEY_UP):
                cursor = max(cursor - 1, 0)
                status = ""
            elif key in (ord(" "), 10, 13, curses.KEY_ENTER):
                if lines[cursor]["kind"] == "checkbox":
                    lines[cursor]["checked"] = not lines[cursor]["checked"]
            elif key == ord("c"):
                text_to_copy = _copyable_text(_render_report_line(lines[cursor]))
                status = (f"[copied: {text_to_copy[:40]}{'...' if len(text_to_copy) > 40 else ''}]"
                         if copy_to_clipboard(text_to_copy)
                         else "[no clipboard tool found — install xclip/xsel/wl-copy/pbcopy]")
            elif key == ord("p"):
                prompt_text = synthesize_ai_prompt(lines, deep)
                if copy_to_clipboard(prompt_text):
                    status = "[AI prompt copied to clipboard — paste into your assistant]"
                else:
                    chosen = _pick_export_path(stdscr, curses, os.getcwd(), "qa-plan-ai-prompt.txt")
                    if chosen:
                        try:
                            with open(chosen, "w") as f:
                                f.write(prompt_text + "\n")
                            status = f"[no clipboard tool — saved prompt to {chosen} instead]"
                        except OSError as e:
                            status = f"[save failed: {e}]"
                    else:
                        status = "[no clipboard tool found, and export cancelled]"
            elif key == ord("w"):
                run_guided_walkthrough(stdscr, curses, lines, teal_attr)
                status = "[walkthrough ended]"
            elif key == ord("e"):
                chosen = _pick_export_path(stdscr, curses, os.getcwd(), default_filename)
                if chosen:
                    try:
                        with open(chosen, "w") as f:
                            f.write("\n".join(_render_report_line(l) for l in lines) + "\n")
                        status = f"[saved {chosen}]"
                    except OSError as e:
                        status = f"[save failed: {e}]"
                else:
                    status = "[export cancelled]"

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
                    help="(default on) scan local checkout for exact test "
                         "commands, curl commands, and visual checks — kept "
                         "as a no-op flag for backward compatibility")
    ap.add_argument("--no-deep", action="store_true",
                    help="skip the local-checkout deep scan (faster, but no "
                         "curl/visual-check/exact-test-file output)")
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
        service_map = build_service_map(args.repo, args.org)
    plan = analyze(files, service_map, repo=args.repo)
    plan["pr_url"] = pr.get("html_url")
    plan["repo"] = args.repo
    with Spinner("Checking cross-PR references and submodule bumps..."):
        plan["referenced_prs"] = find_referenced_prs(args.repo, pr.get("body") or "",
                                                     pr.get("number", 0))
        plan["submodule_bump_details"] = resolve_submodule_bumps(files, args.org)
    if not args.no_deep:
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
        shown_interactively = run_interactive_report(md, default_export, plan.get("deep"))
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
            args.no_deep = True
            run_plan(args)
        elif action == "deep":
            args.no_deep = False
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
