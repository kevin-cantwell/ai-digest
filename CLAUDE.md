# AI Digest — CLAUDE.md

## What this is

A single-script Python project that fetches AI news, scores it, and renders a static HTML page. GitHub Actions runs it hourly and pushes `index.html` to main, served via GitHub Pages.

## Key files

- `fetch.py` — the entire pipeline: fetch → score → Claude re-score → render HTML
- `index.html` — generated output, committed by CI (do not hand-edit)
- `.github/workflows/update.yml` — hourly cron job; commits as "Clarion Bot"

## Running locally

```bash
pip install requests feedparser anthropic
python fetch.py
open index.html
```

Set `ANTHROPIC_API_KEY` to enable Claude Haiku re-scoring and TL;DR generation. Pass `--no-claude` to skip it.

## Architecture notes

- **Sources**: Hacker News (search API), Reddit (JSON API, no auth), RSS feeds
- **Scoring**: heuristic 0–100; items below 40 are dropped; top 30 optionally re-scored by Claude Haiku
- **Sections**: Major Releases & Announcements, Research & Papers, Tools & Open Source, Industry News
- **Model**: `claude-haiku-4-5-20251001` (keep cheap — runs every hour in CI)

## CI / GitHub Actions

- Workflow: `.github/workflows/update.yml`
- Secret required: `ANTHROPIC_API_KEY` (repo secret)
- Commits only when `index.html` changes; commit message format: `digest: YYYY-MM-DD HH:MM UTC`

## Adding sources

- **HN queries**: extend `HN_AI_QUERIES` list in `fetch.py`
- **Subreddits**: extend `REDDIT_SUBS`
- **RSS feeds**: extend `RSS_FEEDS` (tuples of `(label, url)`)

## What NOT to do

- Don't hand-edit `index.html` — it gets overwritten every hour
- Don't upgrade the Claude model to something expensive without updating the hourly cost math
- Don't add dependencies beyond `requests`, `feedparser`, `anthropic` without updating the workflow install step
