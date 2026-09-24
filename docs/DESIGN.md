# Design notes

This document records the load-bearing design decisions and the incidents that forced them. If you change behavior in `radio_batch.py`, read the relevant section first — most of the non-obvious code exists because the obvious version failed in production.

## Architecture at a glance

A single ~1,100-line Python module, run twice daily by GitHub Actions cron. No server, no database. Persistent state is one JSON file (`state.json`) committed back to the repository by the bot after every run. NotebookLM is driven by shelling out to the [notebooklm-py](https://github.com/teng-lin/notebooklm-py) CLI; credentials are inherited via the environment and never read by this code.

Per run: poll feeds → decide what's new → one notebook per topic per day → add articles as sources → wait for ingestion → remove junk sources → trigger the Audio Overview (fire-and-forget) → notify → advance read-state → maintenance (cleanup, stale-feed report).

## Read-state: two mechanisms, deliberately different

This is the heart of the project. There are two kinds of feeds and each gets a different read-state mechanism. **They must not be swapped** — each one is provably wrong for the other feed type.

### RSS feeds use a watermark

```json
{ "https://vercel.com/blog/feed": { "watermark": "2026-07-09T00:00:00+00:00", "recent_ids": ["..."] } }
```

The watermark is the publish time of the newest article ever processed. An entry is unread iff its timestamp is newer.

**Why not a seen-ID list?** Because an ID list cannot be simultaneously *bounded* and *correct* for RSS. Vercel's feed returns 1,325 entries; any list you trim will "forget" old entries, and a forgotten entry that is still in the feed comes back as unread — forever. This is not hypothetical: the original ID-list implementation produced **1,226 falsely-new articles in one run**, followed by a permanent two-run oscillation as trimmed halves of the feed took turns looking new. A timestamp is monotonic; it doesn't care how many entries the feed returns or how many IDs you throw away.

`recent_ids` exists only for the two cases a timestamp cannot decide: entries sharing the watermark's exact timestamp, and entries with no publish time at all. Neither kind of ID is allowed to fall off the end of the list — the watermark has no opinion about them, so the list is their only read-marker.

**An entry at exactly the watermark time is unread unless its ID is in `recent_ids`**, and the list records only the same-timestamp entries that were *actually processed* (plus everything on a feed's first run). The earlier version did the opposite — it treated `ts == watermark` as read and recorded *every* entry at that time — which is fine for feeds with real timestamps and silently destructive for date-only feeds (Cloudflare Changelog, Vercel, Codex), where every article of a day shares one midnight timestamp: on 2026-09-22 Cloudflare published 7 changelog entries, the per-feed limit took 3, and the other 4 were marked read without ever being processed. A late post the same day would have been lost the same way.

**Two feeds that publish the same URL** (Vercel's `blog/feed` and `atom` are identical) are deduplicated by URL before selection; the article is added once, and read-state advances in both feeds because `advance_state` matches processed articles by URL as well as by ID (GUIDs differ per feed).

### Sitemap feeds use a seen-URL set

```json
{ "sitemap:https://www.anthropic.com/sitemap.xml:https://www.anthropic.com/news/": { "seen": ["..."] } }
```

**Why not a watermark?** Sitemap `<lastmod>` is a *last-modified* date, not a publish date. Editing an old article bumps it past any watermark, and the article resurfaces as "new". This happened on 2026-07-13: an April article reappeared in the radio because someone touched it in July. So `lastmod` is never used for newness.

A seen-set is correct *and* bounded here because a sitemap returns its full URL set every run: membership checks are exact, and the set is intersected with the current sitemap so it can never outgrow it. The sliding-window problem that rules out ID lists for RSS structurally cannot occur.

**The state key includes the URL prefix**, not just the sitemap URL. Two configured feeds can share one physical sitemap (e.g. `/news/` and `/engineering/` on the same domain); when they shared a state key, one feed's articles were silently marked read by the other. That silent loss is why the key is `sitemap:{url}:{prefix}`.

### Invariants that follow

- **Never mark an article read that wasn't processed.** Articles beyond the per-run limits stay unread and are drained oldest-first on later runs.
- **Read-state advances and saves even when there are no new articles** — a feed's first-run initialization must persist. Do not add an early return that skips it.
- **A feed that fails to fetch must not initialize its state.** Otherwise a temporary 404 marks the entire backlog as read.
- **A same-timestamp entry not in `recent_ids` is unread.** `recent_ids` must therefore contain exactly the processed entries at the watermark time — never the unprocessed ones.
- **An article whose sources were added but whose audio could not be started is still marked read.** The sources are already in the notebook; leaving the article unread would only re-add the same URLs next run. The failure is reported separately, with the CLI's error code (e.g. `rate_limited` when the daily Audio Overview quota is hit).
- First run per feed processes only the newest article and marks the rest read — no backlog flood on day one.
- **`mode: latest` is the one deliberate exception** to "never mark unread articles read", and it is opt-in per feed. An aggregator that publishes hundreds of items a month cannot be drained oldest-first at three per run without airing week-old items forever; for such feeds the user chooses "the newest N each run, the rest is read". A run that processes nothing from such a feed (its topic failed) advances nothing, so the next run picks the newest again. Sitemaps ignore it — a seen-set looks at the full URL set every run, so there is no backlog to skip.

## Topic split

Each feed maps to a `topic`, and each topic gets its own daily notebook and its own audio generation. Mixing AI news and infrastructure changelogs in one conversation produced incoherent radio. Topics fail independently: one topic's error doesn't stop the other's episode, read-state advances only for articles whose topic succeeded, and the run exits non-zero at the end if anything failed.

**Per-topic audio.** `topics.<name>.audio` overrides `settings.audio` for one topic (`format`, `length`, `prompt`, `language`), resolved by `audio_settings_for`. A changelog topic can be a short *brief* while the AI topic stays a long *deep-dive*. Cleanup recognizes topic names from this section as well as from `feeds[].topic`, so a topic whose feeds were removed still gets its old notebooks deleted.

**Episodes cover the run, not the day.** Two runs a day share one notebook per topic, but the second Audio Overview is generated with `-s` limited to the sources added in *that* run (`settings.audio.scope: run`, the default). Before this, the evening episode re-discussed every morning article. `scope: notebook` restores the old whole-notebook behavior.

**Quota is the real limit on splitting.** NotebookLM caps Audio Overviews per day (3 on the free tier, 6 on Plus, 20 on Pro). Every topic × every run is one generation, so two topics twice a day already exceed the free tier; the fourth trigger fails with `rate_limited`, the articles stay in the notebook and are marked read, and the failure is reported. Choose the number of topics and runs with that arithmetic in mind.

## Configuration is validated, not trusted

`validate_config` runs before anything else, and `python radio_batch.py --check-config` runs it alone (CI does the same before every batch). Unknown keys are **errors**, not warnings: a `feed:` typo used to yield zero feeds and a cheerful "nothing new today", and a `topics:` inside a feed silently routed it to the default topic. The same rules are published as `config.schema.json` so that a config builder can only produce what the runtime accepts. This is not a dry-run mode for the batch — it reads one file and touches nothing else.

## Cleanup: the full-match guarantee

`cleanup_old_notebooks` deletes notebooks whose title **fully matches** `notebook_title_format` with a `YYYY-MM-DD` date, older than `retention_days`. `{topic}` expands only to topic names that actually exist in the config — never to a wildcard. This full-match rule is the *entire* safety guarantee that your manually created notebooks are untouchable. Never loosen it to substring or prefix matching. `dry_run: true` (the shipped default) posts the would-delete list instead of deleting.

## Hostile fetch targets

Two related problems, one honest policy.

**`source_mode: text`** exists because some hosts (e.g. `openai.com` article pages) return a bot challenge (`403`, `cf-mitigated: challenge`) to every non-browser client, so NotebookLM's fetcher stores the challenge page instead of the article. Retries can never fix this — the block is on their side. Such feeds are submitted as text sources built from the RSS title + summary, which never touches the fetcher. The text **must** keep its leading disclaimer that it is a summary, not the full article: some feeds ship only a ~150-character description, and a radio that assumes it read the full article will invent facts. Notifications mark these articles as summary-only.

**Junk-source detection**: `source add` can "succeed" while storing a challenge page — the source becomes ready with a title like "Just a moment...". After the ingestion wait, sources whose titles match the challenge-page regex are deleted and reported as not-included. If *all* of a topic's sources were junk, audio generation is skipped (regenerating from the notebook's stale sources would be worse than silence), but the articles are still marked read — retrying would fetch the same challenge page. Text-mode sources are excluded from this check: their titles are ones we set ourselves.

**The policy**: this project does **not** spoof a browser User-Agent to get past bot protection. That would be evading someone else's explicit access decision. We settle for less content instead, and we say so honestly in the output. The fetch User-Agent identifies this software and links back to its repository.

## Things this project deliberately does not do

Documented so they aren't "fixed" casually:

- **No backend abstraction over the notebooklm CLI (yet).** All CLI calls sit behind ~8 conceptual operations (list/create/delete notebook, add/wait/list/delete source, generate audio), so an adapter seam exists on paper. It stays unbuilt until a concrete second backend (e.g. an official API) gives it a reason to exist. Abstractions built for one implementation are speculation.
- **Not a pip library.** notebooklm-py owns the client-library niche; this is an application template. The value is the pipeline and its operational hardening, not an importable API.
- **No notification plugin system.** Slack and Discord are auto-detected from the webhook URL; that covers the realistic cases at near-zero complexity.
- **No dry-run mode for the batch itself.** The script's side effects are the product. Verification is `pytest -q` (no network — the CLI boundary and feed fetching are monkeypatched); real execution happens only via `gh workflow run`. `--check-config` is not a dry run: it validates `config.yaml` and exits without touching feeds, NotebookLM or state.
- **No User-Agent spoofing.** See above.

## Known weaknesses (and where they lead)

Being honest about these is part of the design; each one points at a future improvement rather than hiding behind one.

| Weakness | Mitigation today | Where it leads |
|---|---|---|
| Credentials expire ~every 3.5 weeks, by hand | Error notification names the cause + runbook | A scheduled read-only canary probe that warns *before* the morning episode dies |
| Whole pipeline rests on an unofficial API | Honest disclaimer; version pinned | A pluggable backend (official LLM + TTS APIs) — the day that exists, the adapter seam above gets built |
| Audio quality and hallucination risk are NotebookLM's | Summary-only sources carry explicit disclaimers | Better prompts; a prompt collection is a natural first contribution |
| `--no-wait` means a failed render is invisible | Notification honestly says "generation started" | Next-run verification of the previous episode's artifact |
