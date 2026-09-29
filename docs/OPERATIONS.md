# Operations

How to keep this running, what breaks, and the incidents that shaped these rules.

## The auth model (read this once, it explains everything else)

NotebookLM has no official API. Authentication is a set of Google session cookies captured by a real browser login (`notebooklm -p ci login`) and frozen into the `NOTEBOOKLM_AUTH_JSON` secret.

The crucial asymmetry: a **local** login stays alive because every CLI call rewrites `~/.notebooklm/profiles/<name>/storage_state.json` with rotated cookies. The **secret** is a frozen copy with no backing file — notebooklm-py's cookie-refresh and recovery paths explicitly decline for env-var auth (there is nowhere to write the rotated cookies back to). So the secret's session ages from the moment you capture it and eventually dies. **This cannot be fixed by retries, waiting, or code** — only by re-capturing.

Measured lifetime: **~3.5 weeks** — *if* you follow the profile rule below.

## Rule: the CI credential comes from a dedicated profile, never your default

Use `notebooklm -p ci login` and export the `ci` profile. Never point the secret at `profiles/default`.

Why: the two profiles are separate Google sessions (verified: `SID` / `__Secure-1PSID` / `__Secure-1PSIDTS` / `SAPISID` all differ) on the same account. If the secret shares your everyday session, every local `notebooklm` call rotates the cookies and invalidates the frozen copy — measured lifetime drops from ~3.5 weeks to **~3 days**. Keep `default` as your active local profile so ordinary use never touches the CI session.

## Renewal runbook (~2 minutes)

When auth expires (or the error notification says so):

```bash
python scripts/setup.py renew
```

That runs `notebooklm -p ci login`, streams `~/.notebooklm/profiles/ci/storage_state.json` into the `NOTEBOOKLM_AUTH_JSON` secret (through stdin, so it works in PowerShell too), records the date in the `NOTEBOOKLM_AUTH_UPDATED` repository variable, and offers to re-run the workflow. The manual equivalent:

```bash
notebooklm -p ci login
gh secret set NOTEBOOKLM_AUTH_JSON < ~/.notebooklm/profiles/ci/storage_state.json
```

Then re-run the workflow (Actions → Run workflow) or wait for the next cron. `python scripts/setup.py doctor` shows the credential's age at any time.

**On `--master-token`.** notebooklm-py 0.8.0 added `notebooklm login --master-token`, a long-lived account-level token meant for cron recovery. Its own source describes it as full-account, durable and "infostealer-grade", to be used with a dedicated throwaway account only. That is a legitimate way to escape the 3.5-week renewal — *if* the radio runs under a throwaway Google account you would not mind losing, which also means a free-tier NotebookLM quota (3 Audio Overviews/day). This project does not use it by default and does not recommend it for your main account.

## How expiry actually presents (an upstream quirk worth knowing)

An expired env-var credential makes the CLI exit **2 / `UNEXPECTED_ERROR`**, not 1 / `AUTH_ERROR` — the expiry lands in the library's unhandled-exception bucket, so the exit code alone looks like a bug, not a credential problem. This exact misdirection cost a 16-day silent outage (see incident log). Two defenses now exist:

- `describe_failure` parses the CLI's JSON error envelope from stdout and puts `code` + `message` into the notification, so the text names authentication instead of just "exit 2".
- When the message matches auth expiry, the notification appends the runbook above verbatim.

## Secret hygiene (non-negotiable)

- **Never put raw subprocess stdout/stderr in a notification.** It can carry session cookies, and GitHub's log masking does **not** apply to webhook payloads. `describe_failure` copies only the two known envelope fields (`code`, `message`); everything user-facing additionally passes through `redact()`, which strips the secrets' values and every long string found inside the auth JSON.
- **The workflow checks out with `persist-credentials: false`.** notebooklm-py is third-party code running with your NotebookLM session in its environment; it must not also be able to reach a git write token. The state push at the end uses an ephemeral authenticated remote instead.
- **Dependencies are pinned on purpose** (`requirements.txt` exact versions, `notebooklm-py==0.8.0`, the setup-uv action pinned by SHA). Bump them deliberately, not implicitly.

## CI environment gotchas

- **Only `$HOME/.local/bin` goes into `$GITHUB_PATH`.** Adding a uv-internal venv bin path shadows the pip-installed packages and makes `feedparser`/`httpx`/`yaml` unimportable. The workflow's "Verify environment" step exists to catch this regressing.
- **Cron minutes are deliberately off the half-hour** (`:50`, `:15`) — GitHub's cron is congested at :00/:30 and delays runs.
- **`state.json` is bot-owned.** CI commits and pushes it after every run (`chore: update state.json [skip ci]`). Never hand-edit or commit it yourself. Deleting it resets every feed to first-run behavior (latest article only, backlog marked read) — that is the recovery of last resort, not a routine action.
- **Notebook titles use the configured timezone**, because runners are UTC and a `date.today()` would file the morning episode under yesterday.

## Reading the notifications

| Signal | Meaning |
|---|---|
| 🎙️ article list | Episode generation **started** (not finished — audio is fire-and-forget). Each topic heading links to its notebook. A heading marked 音声は未生成 means the sources are in but no audio was started for that topic — see the separate error message. |
| 😪 no news | The run worked; there was nothing new. Silence, by contrast, means breakage. |
| ⚠️ feed warning | A feed failed to fetch/parse (its read-state was left untouched), or a per-run check fired: every visible entry is unread (older ones may have scrolled out before being processed — raise the limits or use a narrower feed), links point at `#anchors` on one page (use `source_mode: text`), or a sitemap's URLs changed all at once (re-baselined, nothing aired). |
| ⏭ skipped as stale | Articles older than `settings.max_age_hours`, marked read instead of aired. Listed so nothing disappears silently. |
| 🗓 weekly check | Sunday: feed health, what aired versus what the feeds published (not aired, aired twice, still pending) and, with `weekly_check.recall`, the Deep Research sweep for announcements no feed carried. Two lines when all is well. A "not aired" article is a loss nobody has explained yet — worth a look. |
| ⚠️ bot-protection notice | Articles whose content couldn't be ingested; they are *not* in the episode |
| 🧹 cleanup report | What was (or in dry-run, would be) deleted; a 🧹 削除に失敗 line means cleanup failed and will retry next run |
| 🚨 gate failure | The test/lint job failed, so the batch did not run at all. Fix the code; there is no episode until it is green. |
| 🩺 monthly health check | Feeds with no new articles for 30+ days |
| ⚠️ error + runbook | Something failed; if it's auth expiry, the fix is written in the message. If it says the audio could not be started, the articles are already in the notebook (and marked read) — generate the Audio Overview by hand in the app; `rate_limited` means the daily quota (3 free / 6 Plus / 20 Pro). |

## Incident log

The design rationale in this repo is earned. Dates preserved from the original private deployment.

**2026-07 — the 1,226-article flood.** The original seen-ID read-state, trimmed to stay bounded, forgot entries that were still present in Vercel's 1,325-entry feed. One run re-announced 1,226 "new" articles, then the feed's trimmed halves oscillated between runs indefinitely. Fix: watermark read-state for RSS (`docs/DESIGN.md`). The regression test for this is the first test in the suite.

**2026-07-13 — the resurrected April article.** Sitemap feeds then used the same watermark logic; an old article's `<lastmod>` was bumped by an edit and it aired as news three months late. Fix: seen-URL sets for sitemaps; `lastmod` is never treated as newness.

**2026-07 — the shared sitemap key.** Two feeds reading different prefixes of the same sitemap shared one state entry; one feed's articles were silently marked read by the other. Fix: the prefix became part of the state key.

**2026-07-23 → 2026-08-01 — the first credential expiry.** The initial CI secret lasted about 15 days; every run failed with exit 2 and an error notification, and the cause (expiry) was only understood after the second, worse outage below.

**2026-08-05 → 2026-08-21 — the 16-day silence.** The frozen CI credential expired; the CLI exited 2/`UNEXPECTED_ERROR`, which read as "some bug" rather than "credentials", and the batch died quietly every day. Fixes: error envelope parsing in notifications, the auth-expiry hint with the runbook, the no-news heartbeat (so silence is always abnormal), and the dedicated `ci` profile (the first replacement secret, shared with the default profile, died in 3 days).

**2026-08-11 → 08-13 and 2026-08-21 → 08-30 — the silent gate.** The batch job depends on the test job. A date-dependent test, then three lint errors in files the batch does not even use, failed the gate for 25 runs; the batch never ran, so there was no error notification and no heartbeat — the one kind of silence the heartbeat was meant to make impossible. Fix: the test job now posts a 🚨 message to the webhook when it fails.

**2026-08 — state lost on failed runs.** The state-push step only ran when the batch succeeded, but `main()` saves read-state for the topics that succeeded before exiting non-zero. During the auth outage the same 11 watch-page updates were re-announced on every failed run. Fix: the push step runs whenever the batch step ran, success or failure.

**2026-09-22 — the same-timestamp loss.** Cloudflare Changelog published 7 entries in one day; date-only feeds give them all the same midnight timestamp. The per-feed limit processed 3, and the other 4 were recorded as read because the code treated *every* entry at the watermark time as a tie. Fix: an entry at the watermark time is unread unless its ID was actually processed (`docs/DESIGN.md`).

**2026-09 — the Vercel double.** `vercel.com/blog/feed` and `vercel.com/atom` return identical content. Every Vercel article was added as two sources, listed twice, and used two of the 15 per-run slots, on 26 of 27 runs with articles. Fix: cross-feed URL deduplication; both feeds still advance.

**2026-08-20 → 09-13 — the starved window.** After the credential outage, several feeds had a backlog of old articles, and the per-run total (15) was filled strictly oldest-first. GitHub Changelog shows only its newest 10 items (about a day); it had 10 unread items on every run from 09-01 to 09-13, got zero slots in 30 capped runs, and 101 of its 152 posts scrolled out of the feed before being processed (Google Cloud lost 32 the same way, 135 in total). Fix: the total cap is filled by how close an article is to scrolling out of its feed, then by age.

**2026-09 — backdated late arrivals.** Feeds insert items dated earlier than items they have already published (OpenAI on 09-23: seven posts dated 01:00–12:00 appeared after a 13:00 post had been processed; Cloudflare's changelog back-fills entries up to two days late). Each was older than the watermark on arrival and was read without being processed — 37 in September. Fix: a 7-day lookback below the watermark, gated by a record of every processed ID.

**2026-09 — re-dated articles aired twice.** Vercel re-dated an edited post, which re-crossed the watermark and aired again three days later; Claude Code's what's-new feed changed an old week's date (and its content-hash GUID) and aired it as new. Fix: every processed ID is remembered (the what's-new feed also moved to per-week pages read from the sitemap).

**2026-09-29 — the trailing slash.** antigravity.google changed its sitemap URLs to end in `/`. None matched the stored seen-set, so all 24 posts looked new, one old post aired, and the seen-set was overwritten with the single aired URL. The feed was paused the same day. Fix: seen-set comparison ignores a trailing slash, and a sitemap where more than half the URLs turn unread at once is re-baselined with a warning instead of aired.

**2026-09 — anchor-linked changelogs imported whole pages.** The Codex changelog links every entry to a `#fragment` of one ~1.4 MB page; NotebookLM imports the page, so 42 adds in a month were 21+ copies of the entire history (three in one notebook). Fix: `source_mode: text` submits each entry's own text, labeled as the feed's body rather than as a summary.

**2026-09 — audio failure left articles unread.** If `generate audio` failed after the sources were added, the topic counted as failed and its articles stayed unread, so the next run re-added the same URLs. Fix: those articles are marked read and the failure is reported separately with the CLI's error code, so a daily-quota rejection (`rate_limited`) is visible instead of an opaque exit code.
