# notebooklm-radio user guide

[日本語](GUIDE.ja.md)

One document from first setup to daily use to troubleshooting. Design rationale lives in [DESIGN.md](DESIGN.md); operational detail and the incident log in [OPERATIONS.md](OPERATIONS.md). This guide only points there.

## Contents

1. [What it does, what it doesn't](#1-what-it-does-what-it-doesnt)
2. [What you need](#2-what-you-need)
3. [Build your config](#3-build-your-config)
4. [Create the repository and drop the config in](#4-create-the-repository-and-drop-the-config-in)
5. [Run the setup wizard](#5-run-the-setup-wizard)
6. [Every morning](#6-every-morning)
7. [Changing things](#7-changing-things)
8. [Operations: the 3.5-week renewal and cleanup](#8-operations-the-35-week-renewal-and-cleanup)
9. [When something looks wrong](#9-when-something-looks-wrong)
10. [Trying it out first](#10-trying-it-out-first)
11. [Terms](#11-terms)

## 1. What it does, what it doesn't

**Does**

- Polls your RSS/Atom/sitemap feeds twice a day (times you choose), adds only the new articles to a Google NotebookLM notebook, and starts an Audio Overview (a two-host conversational show).
- One notebook per topic per day, so an AI-news show and a cloud-changelog show stay separate. Each topic can have its own conversation format, length and instructions.
- Posts today's article list, with a link to each notebook, to Slack or Discord.
- Deletes old notebooks after 7 days (as a dry run at first: it only announces what it would delete).
- No server, no database. GitHub Actions cron only.

**Doesn't / caveats**

- NotebookLM has no official API. This uses the unofficial CLI (notebooklm-py) with *your* Google session. Google can break it without notice, and the session must be renewed by hand about every 3.5 weeks ([section 8](#8-operations-the-35-week-renewal-and-cleanup)).
- Audio Overviews have a daily cap: 3 on the free tier, 6 on Google AI Plus, 20 on Pro. Notebooks × runs per day is your daily count.
- Generated audio is for personal listening. The source articles' copyright and NotebookLM's terms make public redistribution a grey area this project doesn't cater to.
- Hosts that block automated fetchers (openai.com, for one) only contribute the feed's summary text, and the show says so.

## 2. What you need

| What | For | Check |
|---|---|---|
| A GitHub account | The repository and GitHub Actions | |
| A Google account with NotebookLM | Notebooks and audio | https://notebooklm.google.com opens |
| A Slack or Discord webhook URL | Notifications; [section 5](#5-run-the-setup-wizard) says where to get one | Optional, but without it you also get no error reports |
| A machine with a browser | The one-time login and each renewal | |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | Installing the notebooklm CLI | `uv --version` |
| [GitHub CLI](https://cli.github.com/) | Setting secrets, triggering runs | `gh auth status` |
| Python 3.10+ | The wizard (standard library only) | `python3 --version` |

The wizard in [section 5](#5-run-the-setup-wizard) tells you which of these are missing, so you don't have to get everything right first.

## 3. Build your config

The whole configuration is one file, `config.yaml`. You can write it by hand, but the **[config builder](https://inoueuj.github.io/tech-feed-catalog/)** warns you about the things that bite later. It runs entirely in your browser and sends nothing anywhere, except that checking a feed outside the catalog is done by that site's worker on your behalf.

**Step 1 — pick feeds.** Tick feeds from a catalog of 76 that CI re-validates weekly. ★★★ means long-form posts that make good radio; ★ means one-line changelog entries. Anything not listed can be added at the top: a Zenn topic, a Zenn user, a Qiita tag, or any feed URL. **Check** fetches it once and reports whether it really is a feed, how many entries it returns, whether they carry timestamps, and roughly how many posts per month.

**Step 2 — group into notebooks.** Feeds land in a box per topic. Rename boxes freely and move feeds between them with the dropdown. The rule of thumb: would these sound right in one show? Model announcements and Cloudflare changelog entries don't. Per box, choose the conversation format (deep-dive / brief / critique / debate), the length, and the focus instructions; the default instructions are built from "your stack" in step 3. Firehose feeds (Hacker News, aggregators) get **newest only** automatically: take the newest N each run and mark the rest read, instead of airing week-old items forever.

**Step 3 — basics.** Language, timezone, run times, and your NotebookLM plan. The plan shows "notebooks × runs = Audio Overviews per day" and turns red when you exceed the cap; merge notebooks or turn off the second run.

**Step 4 — output.** The `config.yaml` tab is the complete file: **Download** it. The `cron` tab has the two schedule lines already converted to UTC (GitHub's cron only speaks UTC). **Share link** copies a URL that restores the whole setup; the same browser also remembers it across reloads.

The "things to know" panel lists: feeds CI can't fetch, hosts that refuse bots, two feeds that publish the same articles (subscribing to both adds every article twice), summary-only feeds, and volumes your limits can't absorb.

## 4. Create the repository and drop the config in

1. Open https://github.com/inoueUJ/notebooklm-radio → **Use this template → Create a new repository**. Any name; choose **Private** (the bot commits your reading history as `state.json`).
2. Clone it: `gh repo clone you/your-repo`
3. Replace `config.yaml` **entirely** with the downloaded file. Don't paste it under the existing `feeds:`.
4. In `.github/workflows/rss-radio.yml`, replace the two `- cron:` lines under `schedule:` with the ones from the `cron` tab.
5. Validate. This reads the file and lists problems; it runs nothing:

```bash
python radio_batch.py --check-config
```

`config.yaml OK: 8 feed(s), topics: AI, Dev, Infra` means it passed. CI runs the same check before every batch, so a typo never silently falls back to defaults. If it fails locally for lack of packages, `pip install -r requirements.txt` fixes that, or just let CI check it.

6. Commit and push. Nothing runs yet (without the secrets it stops immediately).

## 5. Run the setup wizard

Inside the clone:

```bash
python scripts/setup.py
```

It narrates what it does:

1. **doctor** — Python, repository, uv, gh login, notebooklm CLI, whether a login exists and how old it is, which secrets are set, config.yaml validity. Missing items come with the fix.
2. **Install the notebooklm CLI** with uv, pinned to the version CI uses.
3. **Google login** — runs `notebooklm -p ci login`; a browser opens. Sign in with the Google account that has NotebookLM. This lands in a dedicated `ci` profile, kept apart from your everyday one; sharing them shortens the credential's life from ~3.5 weeks to ~3 days ([OPERATIONS.md](OPERATIONS.md)).
4. **Store the secret** — the credential file becomes `NOTEBOOKLM_AUTH_JSON`. Its contents never appear on screen or in shell history. The date is recorded as a repository variable.
5. **Store the webhook** — you're asked for the URL (hidden input). Empty skips it, and you get no notifications at all.
6. **First run** — it offers to trigger the workflow right away.

Where the webhook comes from:
- **Slack**: https://api.slack.com/apps → Create New App → From scratch → Incoming Webhooks → On → Add New Webhook to Workspace → pick a channel → copy the `https://hooks.slack.com/services/...` URL.
- **Discord**: channel settings → Integrations → Webhooks → New Webhook → Copy Webhook URL.

`python scripts/setup.py doctor` runs the checks alone and changes nothing. `python scripts/setup.py renew` is the 3.5-week renewal ([section 8](#8-operations-the-35-week-renewal-and-cleanup)).

If it doesn't go through:
- "workflows are disabled" — a repo made from a template may have Actions off. Enable them in the Actions tab, then re-run the wizard or `gh workflow run rss-radio.yml`.
- `gh` not logged in — `gh auth login`, then retry.
- `notebooklm` not found — open a new terminal (the install adds `~/.local/bin` to PATH).
- PowerShell works the same way; the wizard never uses a `<` redirect.

## 6. Every morning

Each run posts something like:

```
🎙️ Anthropic、Cloudflare が新しい記事出してたよ（5件）
ラジオの生成を開始したよ〜
《AI》 <Tech Radio AI 2026-09-26>
・<article title>（Anthropic）
・<article title>（OpenAI・要約のみ）
《Infra》 <Tech Radio Infra 2026-09-26>
・<article title>（Cloudflare）
```

(The notification text is Japanese; the article links and notebook links are what matter.)

- The heading links open the notebook in NotebookLM. No hunting by name.
- "Started", not "finished": NotebookLM renders the audio over the next 10–20 minutes. Turn on the NotebookLM app's notifications to hear when it's done.
- `要約のみ` marks an article whose page is bot-protected; only the feed's summary went in, and the show knows that.
- `（音声は未生成）` on a heading means the sources are in but the audio couldn't be started; a separate message says why (usually the daily cap). You can generate it by hand in the app.
- A quiet run posts `😪 今回は新着なしだったよ`. **Silence is abnormal** ([section 9](#9-when-something-looks-wrong)).
- The second run of a day makes a separate episode from that run's new articles only; the morning's aren't replayed.
- The very first run processes only the newest article per feed and marks the backlog as read. No flood.

After about a week you'll see `🧹 [ドライラン] 7日より古いラジオ 2件を削除する予定` listing notebooks it would delete. If the list is only old episodes, set `cleanup.dry_run: false` in `config.yaml`. Deletion only ever touches notebooks whose title exactly matches this batch's naming; hand-made notebooks are never candidates.

## 7. Changing things

Everything is in `config.yaml`. Commit and push; the next run picks it up (a push doesn't trigger a run; `gh workflow run rss-radio.yml` does).

| Want to | Change |
|---|---|
| Add or remove a feed | Edit `feeds:`. Or rebuild on the builder and replace the whole file (keep its Share link) |
| Move a feed to another show | Its `topic:` |
| A show's format, length, instructions | `topics:` → that topic → `audio:` (`format` / `length` / `prompt`) |
| Instructions for every show | `settings.audio.prompt` |
| Run times | The two `- cron:` lines in `.github/workflows/rss-radio.yml` (UTC); the builder's `cron` tab converts |
| Articles per run | `settings.limits.per_feed` (default 3) and `total` (default 15). Overflow carries over, never dropped |
| A firehose feed | `mode: latest` on that feed |
| Audio language | `settings.language` |
| How long notebooks live | `settings.cleanup.retention_days` |
| A host that blocks fetchers | `source_mode: text` on that feed |

Validate with `python radio_batch.py --check-config`. Every accepted key is in [`config.schema.json`](../config.schema.json); unknown keys are errors on purpose.

A newly added feed's first run processes only its newest article.

## 8. Operations: the 3.5-week renewal and cleanup

**Credential renewal (about every 3.5 weeks).** Your local Google session keeps itself alive; the frozen copy in the secret does not, and dies after ~3.5 weeks. You get an error notification containing `Authentication expired` and the fix. The fix is one command:

```bash
python scripts/setup.py renew
```

It re-logs you in, replaces the secret, and offers to re-run. About two minutes. `python scripts/setup.py doctor` shows the credential's age any time (it warns past 21 days).

Rules:
- Don't use `-p ci` for everyday local `notebooklm` use; that's what keeps the CI session alive for weeks instead of days.
- `state.json` belongs to the bot. Never edit or commit it. Deleting it resets every feed to first-run behavior (last resort only).

**Cleanup.** With `cleanup.dry_run: false`, the morning run deletes episodes older than `retention_days` and reports `🧹 … 削除したよ`.

**Monthly health check.** On the 1st, feeds with nothing new for 30 days are reported with `🩺`. That's how dead feeds and changed site structures show up.

**Actions minutes.** Runs take 1–4 minutes, so twice a day fits comfortably in a private repo's free 2,000 minutes a month.

## 9. When something looks wrong

| You see | Meaning | Do |
|---|---|---|
| Nothing for days | The notification path itself is down | Check the last run in the Actions tab; check `NOTIFY_WEBHOOK_URL` with `doctor` |
| `🚨 テスト/lint が失敗した…` | The code checks failed; no episode was made | Open the linked run. A config problem is printed there |
| `Tech Radio 実行エラー発生` + `Authentication expired` | Credential expired | `python scripts/setup.py renew` |
| `Tech Radio 実行エラー発生` + `ConfigError` | Bad config.yaml | Fix the listed items; `--check-config` |
| `（音声は未生成）` + a message with `rate_limited` | Daily Audio Overview cap | Fewer notebooks, drop the second run, or a bigger plan. Sources are in the notebook; generate by hand if you like |
| `⚠️ RSSフィードの取得に問題があるよ ・X: HTTP 403` | That feed couldn't be fetched; its read-state was left alone | Transient: ignore. Persistent 403: the host refuses bots |
| `⚠️ 以下はサイトのボット対策で本文を取り込めなかった` | Article pages were challenge pages | Add `source_mode: text` to that feed |
| `😪 今回は新着なし` every time | Genuinely quiet, or `feeds:` is empty | `--check-config` prints the feed count |
| The same article twice | Two feeds publish the same articles (Vercel blog + atom) | Drop one; the builder warns about this |
| Only old articles | Volume exceeds the per-feed limit and the backlog drains oldest-first | `mode: latest` on that feed, or raise `limits.per_feed` |
| No audio appears in the notebook | Generation started but failed on Google's side | Regenerate in the app; if it repeats, suspect the cap or the content |
| The evening episode landed in tomorrow's notebook | GitHub started the run hours late and it crossed midnight | Move the cron earlier; avoid :00 and :30, which are the most congested |

## 10. Trying it out first

To experience the whole thing without touching a production setup (this is also the author's own verification path):

1. On the builder, pick two or three feeds, one notebook, and turn the second run off (to save Audio Overview quota).
2. Create a private repo from the template with a name like `radio-trial` and drop the config in ([section 4](#4-create-the-repository-and-drop-the-config-in)).
3. Run the wizard with a separate login profile so a real `ci` profile stays untouched:

```bash
python scripts/setup.py --profile trial --no-run
gh workflow run rss-radio.yml
```

4. Watch the run in the Actions tab, the linked list arrive in Slack, the notebook appear in NotebookLM, and the audio show up about 20 minutes later.
5. Afterwards, delete the repo or disable the workflow in the Actions tab. Left alone it runs twice a day and keeps spending the same Google account's daily quota.

What this shows you: the real setup effort, how notifications read, how you find the notebook. What it can't: the 3.5-week expiry (only waiting shows that; `doctor` warns ahead of it).

## 11. Terms

- **Feed**: a site's machine-readable "what's new" list. RSS/Atom normally; sitemap.xml for sites without one.
- **config.yaml**: the one configuration file: feeds, shows (topics), audio settings, cleanup.
- **state.json**: the record of what has been read. Committed by the bot every run.
- **Topic / notebook**: a group of feeds = one show. One notebook per day per topic, one Audio Overview per notebook per run.
- **Audio Overview**: NotebookLM's generated two-host show. Capped per day.
- **cron**: the line that tells GitHub Actions when to run. Always UTC.
- **Secret**: a value only GitHub Actions can read. Two here: `NOTEBOOKLM_AUTH_JSON` and `NOTIFY_WEBHOOK_URL`.
- **Webhook**: a URL that lets outside software post into a Slack or Discord channel.
- **Profile (`ci` / `default`)**: where the notebooklm CLI stores a login. CI and everyday use are kept apart.
- **Builder**: the web page that produces config.yaml and the cron lines. https://inoueuj.github.io/tech-feed-catalog/
- **newest only / `mode: latest`**: for firehose feeds, take the newest N per run and mark the rest read.
