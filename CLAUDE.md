# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`radio_batch.py` polls the RSS/sitemap feeds in `config.yaml`, and when new articles appear it adds their URLs to a Google NotebookLM notebook (one per topic per day), triggers an Audio Overview, and posts a Slack/Discord notification. It runs only as a GitHub Actions cron job (`.github/workflows/rss-radio.yml`) — there is no server or long-running process.

## Never run the script locally

Do not execute `python radio_batch.py`. It has real side effects: it creates NotebookLM notebooks, adds sources, triggers audio generation, and posts to the production webhook. There is no dry-run mode.

To exercise the app for real, dispatch the workflow instead: `gh workflow run rss-radio.yml`. To verify a change, run the tests: `pytest -q`.

## Commands

```bash
pip install -r requirements-dev.txt
pytest -q          # tests run without network; fetch_feed is monkeypatched
ruff check .
ruff format --check .
```

## Design invariants — read before changing behavior

The load-bearing design decisions and their incident history live in:

- `docs/DESIGN.md` — the two read-state mechanisms (RSS watermark vs sitemap seen-set) and why they must never be swapped; topic split; the cleanup full-match safety guarantee; `source_mode: text` and junk-source detection.
- `docs/OPERATIONS.md` — the auth model (why the secret expires and cannot renew itself), CI profile separation, secret hygiene rules, and the incident log.

Do not regress the invariants documented there. In particular:

- **`state.json` is bot-owned.** CI commits and pushes it after every run. Never hand-edit or commit it.
- **Never mark an article read that wasn't processed.** Overflow articles carry over; they are not dropped.
- **`advance_state` + `save_state` must run even when there are no new articles.**
- **A feed that fails to fetch must not initialize its state.**
- **Never loosen the cleanup title match** from full-match to substring/prefix.
- **Never put raw subprocess stderr in a notification** — it can carry session cookies. Everything user-facing goes through `redact()`.
- **Only `$HOME/.local/bin` goes into `$GITHUB_PATH`** — a uv-internal bin path shadows the pip-installed packages.

## Conventions

- Conventional-commit messages (`feat:`, `fix:`, `chore:`, `docs:`).
- Python is formatted and linted with `ruff` (config in `pyproject.toml`).
