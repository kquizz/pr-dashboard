# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A live PR dashboard — a Flask backend serving a single-page HTML frontend that displays GitHub PR status for the repos you configure. The backend wraps `check_prs.py` (bundled in the repo, so the dashboard is self-contained) to fetch PR data via the `gh` CLI.

## Configuration

`settings.ini` drives everything (gitignored; copy from `settings.ini.example`). `server.py` loads it via `configparser` with built-in defaults for any missing key, and exposes the client-facing subset at `GET /api/config`. Keys: `[server] port, cache_ttl` · `[frontend] refresh_interval` · `[features] enable_fix_pr, hide_drafts, hide_promote_section, hide_charts, show_others` · `[github] org, user` · `[repos] names, screenshots_required, backend_repo`. The org/user/repos and the `show_others` flag are pushed onto the `check_prs` module at import time (`check_prs.REPOS`, `check_prs.SELF_USER`, `check_prs.SHOW_ALL`).

## Running

```bash
pip install -r requirements.txt
python server.py  # http://localhost:5050
```

## Architecture

- **server.py** — Flask app:
  - `GET /` — serves the static SPA
  - `GET /api/config` — client-facing config (flags, repos, refresh interval)
  - `GET /api/prs` — returns cached PR data (`cache_ttl`)
  - `POST /api/refresh` — invalidates cache and re-fetches
  - `POST /api/fix/<repo>/<n>`, `GET /api/jobs`, `GET /api/jobs/<id>/log` — Fix-PR routes; **all 404 when `enable_fix_pr` is false**
- **static/index.html** — self-contained SPA (HTML + CSS + JS, no build step). Uses Chart.js via CDN. On load it fetches `/api/config`, builds the per-repo Active/Draft cards from `repos`, and applies the feature flags (hides Fix UI / draft sections / promote section / charts; shows author badges when `show_others`). Auto-refreshes every `refresh_interval`s.
- **check_prs.py** — bundled GitHub fetcher. Provides `collect_data()` returning `(all_active, all_drafts)` tuples of `(owner, name, pr, ci, review)`. `server.py` overrides its `REPOS`/`SELF_USER`/`SHOW_ALL` from config at import. Ticket keys for cross-repo grouping match `TICKET_PATTERN` (default `[A-Z][A-Z0-9]+-\d+`).

## Key Concepts

- **Fix-PR**: Runs `claude --dangerously-skip-permissions` to auto-fix and push a PR with no human review. Off by default (`features.enable_fix_pr`); both UI and endpoints disappear when disabled.
- **Promotable**: A draft PR is "ready to promote" when it's green (CI passing, no unresolved comments, mergeable), targets main/master, has screenshots if the repo is in `screenshots_required`, and all cross-repo sibling PRs sharing the same issue/ticket key are also green.
- **Issues**: Active (non-draft) PRs needing action — CI failures, unresolved/conversation comments, merge conflicts.
- PRs are grouped by repo and status (active vs draft) in the UI, driven by the configured `repos` list.
