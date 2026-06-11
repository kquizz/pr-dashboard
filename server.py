#!/usr/bin/env python3
"""Live PR Dashboard — Flask backend that wraps check_prs.py."""

import configparser
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, send_from_directory

# Bundled, self-contained PR fetcher (vendored — see its module docstring).
sys.path.insert(0, str(Path(__file__).parent))
import check_prs  # noqa: E402


def load_config():
    """Load settings.ini, falling back to built-in defaults for any missing key."""
    defaults = {
        "server": {"port": "5050", "cache_ttl": "30"},
        "frontend": {"refresh_interval": "60"},
        "features": {
            "enable_fix_pr": "false",
            "hide_drafts": "false",
            "hide_promote_section": "false",
            "hide_charts": "false",
            "show_others": "false",
        },
        "github": {"org": "your-org", "user": "your-username"},
        "repos": {
            "names": "repo-one, repo-two",
            "screenshots_required": "",
            "backend_repo": "",
        },
    }
    parser = configparser.ConfigParser()
    parser.read_dict(defaults)
    parser.read(Path(__file__).parent / "settings.ini")
    return parser


def _csv(value):
    return [item.strip() for item in value.split(",") if item.strip()]


CONFIG = load_config()
PORT = CONFIG.getint("server", "port")
CACHE_TTL = CONFIG.getint("server", "cache_ttl")  # seconds
REFRESH_INTERVAL = CONFIG.getint("frontend", "refresh_interval")
ENABLE_FIX_PR = CONFIG.getboolean("features", "enable_fix_pr")
HIDE_DRAFTS = CONFIG.getboolean("features", "hide_drafts")
HIDE_PROMOTE_SECTION = CONFIG.getboolean("features", "hide_promote_section")
HIDE_CHARTS = CONFIG.getboolean("features", "hide_charts")
SHOW_OTHERS = CONFIG.getboolean("features", "show_others")
ORG = CONFIG.get("github", "org").strip()
GITHUB_USER = CONFIG.get("github", "user").strip()
REPOS = _csv(CONFIG.get("repos", "names"))
SCREENSHOT_REPOS = set(_csv(CONFIG.get("repos", "screenshots_required")))
BACKEND_REPO = CONFIG.get("repos", "backend_repo").strip()
REPO_REMOTES = {r: f"git@github.com:{ORG}/{r}.git" for r in REPOS}

# Point the vendored fetcher at the configured org/user/repos.
check_prs.SELF_USER = GITHUB_USER
check_prs.SHOW_ALL = SHOW_OTHERS
check_prs.REPOS = [{"owner": ORG, "name": r} for r in REPOS]

app = Flask(__name__, static_folder="static")

# In-memory cache
_cache = {"data": None, "timestamp": 0}

# Fix jobs
JOBS = {}


def build_pr_dict(owner, name, pr, ci, review):
    """Convert a single PR tuple into a JSON-serializable dict."""
    base = pr.get("baseRefName", "")
    requires_screenshots = name in SCREENSHOT_REPOS
    is_green = (
        ci == "passing"
        and review["unresolved"] == 0
        and review["mergeable"] == "MERGEABLE"
    )
    is_independent = base in ("master", "main")
    has_screenshots_ok = not requires_screenshots or review["has_screenshots"]

    return {
        "repo": name,
        "owner": owner,
        "number": pr["number"],
        "title": pr["title"],
        "url": pr["url"],
        "author": pr.get("author", ""),
        "branch": pr.get("headRefName", ""),
        "base": base,
        "draft": pr.get("isDraft", False),
        "ci": ci,
        "unresolved": review["unresolved"],
        "issue_comments": review.get("issue_comments", 0),
        "approvers": review["approvers"],
        "mergeable": review["mergeable"],
        "has_screenshots": review.get("has_screenshots", False),
        "tickets": list(review.get("tickets", [])),
        "failing_checks": review.get("failing_checks", []),
        "comment_details": review.get("comment_details", []),
        "is_green": is_green,
        "is_independent": is_independent,
        "has_screenshots_ok": has_screenshots_ok,
        "requires_screenshots": requires_screenshots,
    }


def compute_promotable(all_prs):
    """Determine which draft PRs are ready to promote."""
    candidates = [p for p in all_prs if p["draft"]]
    all_entries = list(all_prs)  # includes active + draft

    ticket_to_entries = {}
    for entry in all_entries:
        for ticket in entry["tickets"]:
            ticket_to_entries.setdefault(ticket, []).append(entry)

    promotable = []
    waiting = []

    for c in candidates:
        if not (c["is_independent"] and c["has_screenshots_ok"]):
            continue

        cross_repo_tickets = {}
        for ticket in c["tickets"]:
            siblings = ticket_to_entries.get(ticket, [])
            cross = [s for s in siblings if s["repo"] != c["repo"]]
            if cross:
                cross_repo_tickets[ticket] = cross

        if cross_repo_tickets:
            all_partners_green = all(
                s["is_green"] and s["is_independent"] and s["has_screenshots_ok"]
                for siblings in cross_repo_tickets.values()
                for s in siblings
            )
            if c["is_green"] and all_partners_green:
                promotable.append(c)
            elif c["is_green"]:
                blockers = []
                for ticket, siblings in cross_repo_tickets.items():
                    for s in siblings:
                        if not (s["is_green"] and s["is_independent"]
                                and s["has_screenshots_ok"]):
                            blockers.append(
                                f"PR-{s['number']} ({s['repo']})"
                            )
                waiting.append({"pr": c, "blockers": blockers})
        else:
            if c["is_green"]:
                promotable.append(c)

    return promotable, waiting


def compute_issues(all_prs):
    """Find PRs needing action (active non-draft only)."""
    issues = []
    for pr in all_prs:
        if pr["draft"]:
            continue
        ref = f"PR-{pr['number']} ({pr['repo']})"
        if pr["ci"] == "failing":
            issues.append({"pr": pr, "type": "ci_failing", "msg": f"{ref}: CI failing"})
        total_comments = pr["unresolved"] + pr["issue_comments"]
        if total_comments > 0:
            parts = []
            if pr["unresolved"] > 0:
                parts.append(f"{pr['unresolved']} unresolved")
            if pr["issue_comments"] > 0:
                parts.append(f"{pr['issue_comments']} conversation")
            issues.append({"pr": pr, "type": "comments", "msg": f"{ref}: {', '.join(parts)}"})
        if pr["mergeable"] == "CONFLICTING":
            issues.append({"pr": pr, "type": "conflicts", "msg": f"{ref}: merge conflicts"})
    return issues


def fetch_data():
    """Fetch fresh data or return cached."""
    now = time.time()
    if _cache["data"] and (now - _cache["timestamp"]) < CACHE_TTL:
        return _cache["data"]

    all_active, all_drafts = check_prs.collect_data()

    # If every PR came back with unknown CI, the fetch likely hit a rate limit
    # or transient error — don't cache it, return stale data if available.
    all_prs_flat = all_active + all_drafts
    if all_prs_flat and all(ci == "unknown" for _, _, _, ci, _ in all_prs_flat):
        return _cache["data"]  # stale is better than poisoned cache

    all_prs = []
    for owner, name, pr, ci, review in all_active:
        all_prs.append(build_pr_dict(owner, name, pr, ci, review))
    for owner, name, pr, ci, review in all_drafts:
        all_prs.append(build_pr_dict(owner, name, pr, ci, review))

    active = [p for p in all_prs if not p["draft"]]
    drafts = [p for p in all_prs if p["draft"]]
    promotable, waiting = compute_promotable(all_prs)
    issues = compute_issues(all_prs)

    # Stats
    ci_passing = sum(1 for p in all_prs if p["ci"] == "passing")
    ci_pending = sum(1 for p in all_prs if p["ci"] == "pending")
    ci_failing = sum(1 for p in all_prs if p["ci"] == "failing")
    approved_count = sum(1 for p in all_prs if len(p["approvers"]) > 0)

    result = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "prs": {
            "active": active,
            "drafts": drafts,
        },
        "promotable": promotable,
        "waiting": waiting,
        "issues": issues,
        "stats": {
            "total": len(all_prs),
            "active": len(active),
            "drafts": len(drafts),
            "ci_passing": ci_passing,
            "ci_pending": ci_pending,
            "ci_failing": ci_failing,
            "ready_to_promote": len(promotable),
            "approved": approved_count,
        },
        "funnel": {
            "draft": len(drafts),
            "ci_passing": ci_passing,
            "in_review": len(active),
            "approved": approved_count,
        },
    }

    _cache["data"] = result
    _cache["timestamp"] = now
    return result


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/config")
def api_config():
    """Client-facing config so the frontend can adapt without a rebuild."""
    return jsonify({
        "enable_fix_pr": ENABLE_FIX_PR,
        "hide_drafts": HIDE_DRAFTS,
        "hide_promote_section": HIDE_PROMOTE_SECTION,
        "hide_charts": HIDE_CHARTS,
        "show_others": SHOW_OTHERS,
        "refresh_interval": REFRESH_INTERVAL,
        "org": ORG,
        "repos": REPOS,
    })


@app.route("/api/prs")
def api_prs():
    data = fetch_data()
    return jsonify(data)


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """Force cache invalidation and re-fetch."""
    _cache["timestamp"] = 0
    data = fetch_data()
    return jsonify(data)


BACKEND_LABEL = "\U0001f9d1\u200d\U0001f527 Backend work needed"


@app.route("/api/promote/<repo>/<int:pr_number>", methods=["POST"])
def api_promote_pr(repo, pr_number):
    if repo not in REPO_REMOTES:
        return jsonify({"error": "Unknown repo"}), 400

    # Optionally flag PRs whose ticket has a sibling in the configured backend
    # repo, so reviewers know coordinated backend work is needed. Off unless
    # `repos.backend_repo` is set.
    data = _cache.get("data")
    has_backend_siblings = False
    if BACKEND_REPO and data:
        all_prs = data["prs"]["active"] + data["prs"]["drafts"]
        pr_data = next((p for p in all_prs if p["repo"] == repo and p["number"] == pr_number), None)
        if pr_data:
            tickets = set(pr_data.get("tickets", []))
            has_backend_siblings = any(
                p["repo"] == BACKEND_REPO and set(p.get("tickets", [])) & tickets
                for p in all_prs
            )

    result = subprocess.run(
        ["gh", "pr", "ready", str(pr_number), "--repo", f"{ORG}/{repo}"],
        capture_output=True, text=True, timeout=30,
        env=os.environ.copy(),
    )
    if result.returncode != 0:
        return jsonify({"error": result.stderr.strip() or "gh pr ready failed"}), 500

    label_added = False
    if has_backend_siblings:
        subprocess.run(
            ["gh", "label", "create", BACKEND_LABEL,
             "--color", "8B0000", "--repo", f"{ORG}/{repo}"],
            capture_output=True, text=True, timeout=15, env=os.environ.copy(),
        )
        r = subprocess.run(
            ["gh", "pr", "edit", str(pr_number),
             "--add-label", BACKEND_LABEL,
             "--repo", f"{ORG}/{repo}"],
            capture_output=True, text=True, timeout=15, env=os.environ.copy(),
        )
        label_added = r.returncode == 0

    _cache["timestamp"] = 0  # invalidate so next fetch reflects promotion
    return jsonify({"status": "promoted", "backend_label_added": label_added})


def _get_fix_args(repo, pr_number, branch):
    """Return (user_prompt, extra_cli_args) for the claude invocation."""
    remote = REPO_REMOTES[repo]
    pr_url = f"https://github.com/{ORG}/{repo}/pull/{pr_number}"

    user_prompt = f"""Fix all open issues on PR #{pr_number} in repo {ORG}/{repo}.
PR URL: {pr_url}

You are in an isolated temp directory. Clone the PR branch, then work in it:

  git clone --single-branch --branch {branch} {remote} ./{repo}
  cd ./{repo}

Address every unresolved review comment, fix any failing CI, and resolve merge
conflicts (rebase onto the base branch if needed). Commit and push when done.
"""

    return user_prompt, []


def _run_fix(job_id, repo, pr_number, branch):
    workdir = f"/tmp/fix-pr-{job_id}"
    log_path = f"/tmp/fix-pr-{job_id}.log"

    try:
        os.makedirs(workdir, exist_ok=True)
        JOBS[job_id]["workdir"] = workdir
        JOBS[job_id]["log_path"] = log_path

        user_prompt, extra_args = _get_fix_args(repo, pr_number, branch)

        with open(log_path, "w") as log_file:
            proc = subprocess.Popen(
                ["claude", "-p", user_prompt, "--dangerously-skip-permissions"] + extra_args,
                cwd=workdir,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=os.environ.copy(),
            )
            JOBS[job_id]["pid"] = proc.pid
            JOBS[job_id]["status"] = "running"
            proc.wait()

            if proc.returncode == 0:
                JOBS[job_id]["status"] = "done"
            else:
                JOBS[job_id]["status"] = "failed"
                JOBS[job_id]["error"] = f"Exit code: {proc.returncode}"
    except Exception as e:
        JOBS[job_id]["status"] = "failed"
        JOBS[job_id]["error"] = str(e)
    finally:
        JOBS[job_id]["finished_at"] = datetime.utcnow().isoformat() + "Z"


@app.route("/api/fix/<repo>/<int:pr_number>", methods=["POST"])
def api_fix_pr(repo, pr_number):
    if not ENABLE_FIX_PR:
        return jsonify({"error": "Fix-PR is disabled"}), 404
    if repo not in REPO_REMOTES:
        return jsonify({"error": "Unknown repo"}), 400

    # Look up branch from cached data
    data = _cache.get("data")
    branch = None
    if data:
        all_prs = data["prs"]["active"] + data["prs"]["drafts"]
        for pr in all_prs:
            if pr["repo"] == repo and pr["number"] == pr_number:
                branch = pr["branch"]
                break

    if not branch:
        return jsonify({"error": "PR not found in cache — try refreshing first"}), 404

    # Check if already running
    for job in JOBS.values():
        if job["repo"] == repo and job["pr_number"] == pr_number and job["status"] in ("starting", "running"):
            return jsonify({"error": "Already running", "job_id": job["id"]}), 409

    job_id = str(uuid.uuid4())[:8]
    JOBS[job_id] = {
        "id": job_id,
        "repo": repo,
        "pr_number": pr_number,
        "branch": branch,
        "status": "starting",
        "started_at": datetime.utcnow().isoformat() + "Z",
        "finished_at": None,
        "pid": None,
        "log_path": None,
        "error": None,
    }

    thread = threading.Thread(target=_run_fix, args=(job_id, repo, pr_number, branch), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "status": "starting"})


@app.route("/api/jobs")
def api_jobs():
    if not ENABLE_FIX_PR:
        return jsonify({"error": "Fix-PR is disabled"}), 404
    return jsonify(list(JOBS.values()))


@app.route("/api/jobs/<job_id>/log")
def api_job_log(job_id):
    if not ENABLE_FIX_PR:
        return jsonify({"error": "Fix-PR is disabled"}), 404
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    log_path = job.get("log_path")
    if not log_path or not os.path.exists(log_path):
        return jsonify({"lines": []})
    with open(log_path, "r", errors="replace") as f:
        lines = f.readlines()
    return jsonify({"lines": [ln.rstrip() for ln in lines[-100:]]})


if __name__ == "__main__":
    print(f"PR Dashboard running at http://localhost:{PORT}")
    app.run(host="127.0.0.1", port=PORT, debug=False)
