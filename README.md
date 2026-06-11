# PR Dashboard

Live dashboard showing GitHub PR status for the repos you configure. Tracks CI status, review state, merge conflicts, and identifies draft PRs ready to promote.

![Python](https://img.shields.io/badge/python-3.9+-blue) ![Flask](https://img.shields.io/badge/flask-latest-green)

## Quick Start

```bash
cp settings.ini.example settings.ini   # then edit org / user / repos
./run
```

The `run` script installs dependencies and opens the dashboard in your browser.
Requires the [`gh` CLI](https://cli.github.com/) authenticated (`gh auth login`).

## What It Shows

- **Active PRs** — non-draft PRs grouped by repo with CI, review, and merge status
- **Draft PRs** — drafts with promotion readiness indicators
- **Promotable** — drafts that are green, independent, and have all cross-repo siblings green
- **Issues** — PRs needing action (CI failures, unresolved comments, merge conflicts)
- **Stats** — CI pass/fail/pending counts, approval counts, charts (Chart.js)

## Configuration

Copy `settings.ini.example` to `settings.ini` and edit. Every value has a safe
built-in default, so the file only overrides what you set (`settings.ini` is
gitignored — it holds your org/repo names).

| Section | Key | Purpose |
|---|---|---|
| `server` | `port`, `cache_ttl` | Listen port; how long PR data is cached (seconds) |
| `frontend` | `refresh_interval` | Browser auto-refresh interval (seconds) |
| `features` | `enable_fix_pr` | Expose the Fix-PR feature (off by default — see below) |
| `features` | `hide_drafts` | Hide draft sections and the Drafts CI chart |
| `features` | `hide_promote_section` | Hide the "Ready to Promote" section |
| `features` | `hide_charts` | Hide the CI charts row |
| `features` | `show_others` | Show everyone's PRs with an author badge, not just yours |
| `github` | `org`, `user` | GitHub org and the username whose PRs are tracked |
| `repos` | `names` | Comma-separated repos to track and group by |
| `repos` | `screenshots_required` | Repos that must have screenshots before a draft can promote |
| `repos` | `backend_repo` | Repo whose ticket-siblings flag "Backend work needed" on promote (blank = off) |

**Fix-PR is disabled by default.** When on, it runs `claude --dangerously-skip-permissions`
to auto-fix and push PRs with no human review. When off, both the UI and the `/api/fix`
endpoint are gone (the endpoint returns 404).

## Architecture

```
server.py              Flask app (SPA + /api/config, /api/prs, /api/refresh, fix routes)
check_prs.py           GitHub API integration (config-driven org/repos/user)
settings.ini           Your configuration (gitignored; copy from settings.ini.example)
static/index.html      Self-contained SPA (HTML + CSS + JS, no build step)
```

- Self-contained — no external dependencies beyond Flask and the `gh` CLI.
- Backend caches PR data for `cache_ttl`s; frontend auto-refreshes every `refresh_interval`s.
- "Promotable" means: draft, CI green, no unresolved comments, mergeable, targets main/master,
  has screenshots (if the repo is in `screenshots_required`), and all cross-repo sibling PRs
  sharing the same issue/ticket key are also green.

## Manual Run

```bash
pip install -r requirements.txt
python server.py
```
