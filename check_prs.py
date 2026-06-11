#!/usr/bin/env python3
"""
Fetch open PR status (CI, reviews, approvals, conflicts, screenshots, tickets)
from the GitHub API via the `gh` CLI. `collect_data()` is the entry point.

server.py overrides the module-level config below from settings.ini at import
time, so these values are only fallbacks when imported standalone.
"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

# Overridden by server.py from settings.ini.
SELF_USER = ""          # GitHub login whose PRs to show (when SHOW_ALL is False)
SHOW_ALL = False        # When True, include every author's PRs, not just SELF_USER
REPOS = []              # List of {"owner": ..., "name": ...}
TICKET_PATTERN = r"[A-Z][A-Z0-9]+-\d+"  # Issue keys used to group cross-repo siblings


def run_gh(args, timeout=30):
    """Run a gh CLI command and return parsed JSON or raw output."""
    try:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            return None, result.stderr.strip()
        return result.stdout.strip(), None
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except FileNotFoundError:
        print("Error: gh CLI not found", file=sys.stderr)
        sys.exit(1)


def list_prs(owner, name):
    """List open PRs in a repo (SELF_USER's only, unless SHOW_ALL is set).

    Uses GraphQL instead of `gh pr list` because the latter relies on
    GitHub's search index which can go stale and silently drop older PRs.
    """
    query = """
    query($owner: String!, $name: String!, $cursor: String) {
      repository(owner: $owner, name: $name) {
        pullRequests(
          states: OPEN,
          first: 50,
          after: $cursor,
          orderBy: {field: CREATED_AT, direction: DESC}
        ) {
          nodes {
            number
            title
            headRefName
            baseRefName
            isDraft
            url
            author { login }
          }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """
    all_prs = []
    cursor = None
    while True:
        args = [
            "api", "graphql",
            "-f", f"query={query}",
            "-f", f"owner={owner}",
            "-f", f"name={name}",
        ]
        if cursor:
            args.extend(["-f", f"cursor={cursor}"])
        out, err = run_gh(args, timeout=30)
        if err or not out:
            break
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            break
        prs_data = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequests", {})
        )
        for node in prs_data.get("nodes", []):
            author = (node.get("author") or {}).get("login", "")
            if SHOW_ALL or author == SELF_USER:
                all_prs.append({
                    "number": node["number"],
                    "title": node["title"],
                    "headRefName": node["headRefName"],
                    "baseRefName": node["baseRefName"],
                    "isDraft": node["isDraft"],
                    "url": node["url"],
                    "author": author,
                })
        page = prs_data.get("pageInfo", {})
        if page.get("hasNextPage") and page.get("endCursor"):
            cursor = page["endCursor"]
        else:
            break
    return all_prs


def check_ci(owner, name, pr_number):
    """Check CI status for a single PR. Returns (pr_number, status).

    Uses `gh pr view --json statusCheckRollup` since `gh pr checks --json`
    is broken in gh 2.86.0 (parse error on the bucket field).
    """
    out, err = run_gh([
        "pr", "view", str(pr_number),
        "--repo", f"{owner}/{name}",
        "--json", "statusCheckRollup",
        "--jq", ".statusCheckRollup",
    ])

    if err or not out:
        return pr_number, "unknown", []

    try:
        checks = json.loads(out)
    except json.JSONDecodeError:
        return pr_number, "unknown", []

    if not checks:
        return pr_number, "no checks", []

    has_fail = any(
        c.get("conclusion") in ("FAILURE", "TIMED_OUT", "CANCELLED")
        for c in checks if c.get("status") == "COMPLETED"
    )
    has_pending = any(
        c.get("status") in ("IN_PROGRESS", "QUEUED", "PENDING", "WAITING")
        for c in checks
    )

    if has_fail:
        failing_names = [
            c.get("name", "unknown check")
            for c in checks
            if c.get("status") == "COMPLETED"
            and c.get("conclusion") in ("FAILURE", "TIMED_OUT", "CANCELLED")
        ]
        return pr_number, "failing", failing_names
    if has_pending:
        return pr_number, "pending", []
    return pr_number, "passing", []


def batch_review_query(owner, name, pr_numbers):
    """Run a single GraphQL query for all PRs in a repo."""
    if not pr_numbers:
        return {}

    fragments = []
    for num in pr_numbers:
        fragments.append(f"""
    pr{num}: pullRequest(number: {num}) {{
      mergeable
      body
      reviewThreads(first: 100) {{
        nodes {{
          isResolved
          comments(first: 10) {{
            nodes {{ author {{ login }} body }}
          }}
        }}
      }}
      reviews(last: 50) {{
        nodes {{ state body createdAt author {{ login }} }}
      }}
      comments(last: 50) {{
        nodes {{
          author {{ login }}
          body
          reactions(first: 10) {{
            nodes {{ user {{ login }} content }}
          }}
        }}
      }}
      reviewRequests(first: 20) {{
        nodes {{ requestedReviewer {{ ... on User {{ login }} }} }}
      }}
    }}""")

    query = (
        '{ repository(owner: "' + owner + '", name: "' + name + '") {'
        + "".join(fragments)
        + "\n  }\n}"
    )

    out, err = run_gh(["api", "graphql", "-f", f"query={query}"], timeout=30)
    if err or not out:
        return {}

    try:
        data = json.loads(out)
        return data.get("data", {}).get("repository", {})
    except json.JSONDecodeError:
        return {}


def count_unresolved_threads(threads):
    """Count unresolved review threads with comments from others."""
    count = 0
    for thread in threads:
        if thread.get("isResolved", False):
            continue
        for comment in thread.get("comments", {}).get("nodes", []):
            author = comment.get("author", {}).get("login", "")
            if author != SELF_USER and not _is_bot(author):
                count += 1
                break
    return count


def user_has_reacted(comment):
    """Check if SELF_USER has reacted to a comment with any emoji."""
    reactions = comment.get("reactions", {}).get("nodes", [])
    return any(
        r.get("user", {}).get("login", "") == SELF_USER
        for r in reactions
    )


def count_issue_comments(comments, approvers=None):
    """Count issue-level comments that haven't been replied to.

    Walks backwards from newest — once we hit our own comment,
    everything before it is considered addressed. Skips comments
    from users who have already approved the PR (their LGTM
    comments don't need a response). Also skips comments the user
    has reacted to with any emoji (acknowledged).
    """
    approved_set = set(approvers or [])
    count = 0
    for comment in reversed(comments):
        author = comment.get("author", {}).get("login", "")
        if author == SELF_USER:
            break
        if author in approved_set:
            continue
        if _is_bot(author):
            continue
        if user_has_reacted(comment):
            continue
        count += 1
    return count


def get_approvals(reviews):
    """Get list of approvers from review data."""
    latest = {}
    for review in reviews:
        author = review.get("author", {}).get("login", "")
        state = review.get("state", "")
        if author == SELF_USER:
            continue
        if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[author] = state
    return [a for a, s in latest.items() if s == "APPROVED"]


def get_changes_requested(reviews):
    """Get list of reviewers who requested changes."""
    latest = {}
    for review in reviews:
        author = review.get("author", {}).get("login", "")
        state = review.get("state", "")
        if author == SELF_USER:
            continue
        if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[author] = state
    return [a for a, s in latest.items() if s == "CHANGES_REQUESTED"]


BOT_SUFFIXES = ("[bot]",)
BOT_LOGINS = {"blacksmith-sh"}


def _is_bot(login):
    return any(login.endswith(s) for s in BOT_SUFFIXES) or login in BOT_LOGINS


def count_review_body_comments(reviews, approvers=None):
    """Count review-body comments from non-self, non-bot users needing response.

    A review with state COMMENTED and a non-empty body is a substantive
    comment (e.g. Jon's review summaries). We consider it "addressed" if
    SELF_USER posted any review after it (regardless of body content).
    Also skips reviews from users who have approved.
    """
    approved_set = set(approvers or [])
    # Find the timestamp of the latest self-review
    latest_self = None
    for review in reviews:
        author = review.get("author", {}).get("login", "")
        if author == SELF_USER:
            ts = review.get("createdAt", "")
            if ts and (latest_self is None or ts > latest_self):
                latest_self = ts

    count = 0
    for review in reviews:
        author = review.get("author", {}).get("login", "")
        state = review.get("state", "")
        body = (review.get("body") or "").strip()
        ts = review.get("createdAt", "")

        if author == SELF_USER or _is_bot(author):
            continue
        if author in approved_set:
            continue
        if state != "COMMENTED" or not body:
            continue
        # If we posted a review after this one, consider it addressed
        if latest_self and ts and latest_self > ts:
            continue
        count += 1
    return count


def _text_has_images(text):
    """Check if text contains image markdown or HTML img tags."""
    if not text:
        return False
    import re
    return bool(re.search(r'!\[.*?\]\(.*?\)', text)) or \
        bool(re.search(r'<img\s', text, re.IGNORECASE))


def has_screenshots(body, comments=None):
    """Check if PR body or comments have images.

    Checks both the PR body and issue comments (e.g., from gitshot
    which uploads screenshots as individual PR comments).
    """
    if _text_has_images(body):
        return True
    for comment in (comments or []):
        if _text_has_images(comment.get("body", "")):
            return True
    return False


def extract_tickets(title, body):
    """Extract issue/ticket IDs (e.g. ABC-1234) from a PR title and body.

    Looks for TICKET_PATTERN in the title and the first few lines of the
    body (to catch ticket references in the description without picking up
    stray mentions deep in a changelog). These keys group cross-repo
    sibling PRs for promotion readiness.
    """
    import re
    text = title or ""
    if body:
        # Only scan first ~500 chars of body to avoid false positives
        text += " " + body[:500]
    return set(re.findall(TICKET_PATTERN, text, re.IGNORECASE))


def parse_pr_review(repo_data, pr_number, pr_title=""):
    """Parse review data for a single PR."""
    key = f"pr{pr_number}"
    pr_data = repo_data.get(key, {})
    if not pr_data:
        return {
            "unresolved": 0,
            "issue_comments": 0,
            "approvers": [],
            "changes_requested": [],
            "mergeable": "UNKNOWN",
            "has_screenshots": False,
            "tickets": extract_tickets(pr_title, ""),
        }

    threads = pr_data.get("reviewThreads", {}).get("nodes", [])
    reviews = pr_data.get("reviews", {}).get("nodes", [])
    issue_comments_nodes = pr_data.get("comments", {}).get("nodes", [])
    body = pr_data.get("body", "")
    approvers = get_approvals(reviews)
    changes_requested = get_changes_requested(reviews)

    # Filter out stale changes_requested if review has been re-requested
    pending_reviewers = {
        node.get("requestedReviewer", {}).get("login", "")
        for node in pr_data.get("reviewRequests", {}).get("nodes", [])
    }
    changes_requested = [
        r for r in changes_requested if r not in pending_reviewers
    ]

    # Extract comment detail snippets for hover tooltips
    comment_details = []
    for thread in threads:
        if thread.get("isResolved", False):
            continue
        for comment in thread.get("comments", {}).get("nodes", []):
            author = comment.get("author", {}).get("login", "")
            if author != SELF_USER and not _is_bot(author):
                snippet = (comment.get("body") or "").strip()
                # Strip HTML comments and markdown tables for cleaner tooltips
                import re
                snippet = re.sub(r'<!--.*?-->', '', snippet, flags=re.DOTALL).strip()
                snippet = re.sub(r'\|.*?\|', '', snippet).strip()
                # Collapse whitespace
                snippet = re.sub(r'\s+', ' ', snippet)
                if len(snippet) > 120:
                    snippet = snippet[:120] + "..."
                if snippet:
                    comment_details.append(f"{author}: {snippet}")
                break
    # Issue-level (conversation) comments needing response
    approved_set = set(approvers)
    for comment in reversed(issue_comments_nodes):
        author = comment.get("author", {}).get("login", "")
        if author == SELF_USER:
            break
        if author in approved_set:
            continue
        if user_has_reacted(comment):
            continue
        if _is_bot(author):
            continue
        snippet = (comment.get("body") or "").strip()
        import re
        snippet = re.sub(r'<!--.*?-->', '', snippet, flags=re.DOTALL).strip()
        snippet = re.sub(r'\|.*?\|', '', snippet).strip()
        snippet = re.sub(r'\s+', ' ', snippet)
        if len(snippet) > 120:
            snippet = snippet[:120] + "..."
        if snippet:
            comment_details.append(f"{author}: {snippet}")

    return {
        "unresolved": count_unresolved_threads(threads),
        "issue_comments": count_issue_comments(
            issue_comments_nodes, approvers
        ),
        "review_comments": count_review_body_comments(reviews, approvers),
        "approvers": approvers,
        "changes_requested": changes_requested,
        "mergeable": pr_data.get("mergeable", "UNKNOWN"),
        "has_screenshots": has_screenshots(body, issue_comments_nodes),
        "tickets": extract_tickets(pr_title, body),
        "comment_details": comment_details,
    }


def collect_data():
    """Gather all PR data. Returns (all_active, all_drafts).
    Both lists contain tuples of (owner, name, pr_data, ci_status, review_info).
    """
    all_active = []
    all_drafts = []

    # Step 1: Discover PRs (parallel per repo)
    repo_prs = {}
    with ThreadPoolExecutor(max_workers=len(REPOS)) as pool:
        futures = {
            pool.submit(list_prs, r["owner"], r["name"]): r
            for r in REPOS
        }
        for future in as_completed(futures):
            repo = futures[future]
            prs = future.result()
            repo_prs[(repo["owner"], repo["name"])] = prs

    # Separate active vs draft — both get full status checks
    active_by_repo = {}
    draft_by_repo = {}
    for (owner, name), prs in repo_prs.items():
        active_by_repo[(owner, name)] = [
            p for p in prs if not p.get("isDraft", False)
        ]
        draft_by_repo[(owner, name)] = [
            p for p in prs if p.get("isDraft", False)
        ]

    # Step 2: CI checks + GraphQL review queries (parallel for ALL PRs)
    ci_results = {}  # (owner, name, number) -> status
    review_data = {}  # (owner, name) -> repo_data

    all_repo_prs = {}  # merge active + draft for GraphQL batching
    for key in set(list(active_by_repo.keys()) + list(draft_by_repo.keys())):
        all_repo_prs[key] = active_by_repo.get(key, []) + draft_by_repo.get(key, [])

    with ThreadPoolExecutor(max_workers=20) as pool:
        ci_futures = {}
        gql_futures = {}

        for (owner, name), prs in all_repo_prs.items():
            for pr in prs:
                key = (owner, name, pr["number"])
                ci_futures[pool.submit(
                    check_ci, owner, name, pr["number"]
                )] = key

            pr_nums = [p["number"] for p in prs]
            if pr_nums:
                gql_futures[pool.submit(
                    batch_review_query, owner, name, pr_nums
                )] = (owner, name)

        for future in as_completed(ci_futures):
            key = ci_futures[future]
            _, status, failing_checks = future.result()
            ci_results[key] = (status, failing_checks)

        for future in as_completed(gql_futures):
            repo_key = gql_futures[future]
            review_data[repo_key] = future.result()

    # Step 2b: Retry PRs with UNKNOWN mergeable status (GitHub computes lazily)
    unknown_by_repo = {}
    for (owner, name), repo_data in review_data.items():
        for pr in all_repo_prs.get((owner, name), []):
            pr_key = f"pr{pr['number']}"
            pr_data = repo_data.get(pr_key, {})
            if pr_data.get("mergeable", "UNKNOWN") == "UNKNOWN":
                unknown_by_repo.setdefault((owner, name), []).append(pr["number"])

    if unknown_by_repo:
        import time
        for attempt in range(2):
            time.sleep(3)
            with ThreadPoolExecutor(max_workers=len(unknown_by_repo)) as pool:
                retry_futures = {}
                for (owner, name), pr_nums in unknown_by_repo.items():
                    retry_futures[pool.submit(
                        batch_review_query, owner, name, pr_nums
                    )] = (owner, name)
                still_unknown = {}
                for future in as_completed(retry_futures):
                    repo_key = retry_futures[future]
                    owner, name = repo_key
                    new_data = future.result()
                    # Merge resolved entries into review_data
                    for pr_num in unknown_by_repo[repo_key]:
                        pr_key = f"pr{pr_num}"
                        new_pr = new_data.get(pr_key, {})
                        if new_pr.get("mergeable", "UNKNOWN") != "UNKNOWN":
                            review_data[repo_key][pr_key] = new_pr
                        else:
                            still_unknown.setdefault(repo_key, []).append(pr_num)
                unknown_by_repo = still_unknown
                if not unknown_by_repo:
                    break

    # Step 3: Build results
    def build_entries(by_repo):
        entries = []
        for (owner, name), prs in by_repo.items():
            repo_review = review_data.get((owner, name), {})
            for pr in prs:
                ci, failing_checks = ci_results.get(
                    (owner, name, pr["number"]), ("unknown", [])
                )
                review = parse_pr_review(
                    repo_review, pr["number"], pr.get("title", "")
                )
                review["failing_checks"] = failing_checks
                entries.append((owner, name, pr, ci, review))
        return entries

    all_active = build_entries(active_by_repo)
    all_drafts = build_entries(draft_by_repo)

    return all_active, all_drafts
