#!/usr/bin/env python3
"""週 1 回の点検。結果を Slack / Discord に 1 通だけ送る（.github/workflows/weekly-check.yml から実行）。

1. フィードの健康状態: 取得できるか、実行のたびの警告（表示範囲あふれ・#位置リンク）が出ていないか、
   URL で渡しているフィードの最新記事ページが取れるか（ボット対策の確認ページ・中身のないページ）。
2. 突き合わせ: state.json の実行記録（_runs）と今のフィードを比べ、この 1 週間に
   「フィードに載ったのに流していない記事」と「二度流した記事」を数える。
3. 取りこぼし検査（config の weekly_check.recall: true のとき）: NotebookLM の Deep Research に
   この 1 週間の公式発表を探させ、購読しているサイトの記事なのにどのフィードにも無かったものを出す。

state.json は読むだけで書き換えない。NotebookLM で行うのは Deep Research 用のノートブックを 1 冊作ることだけで、
音声は作らない（そのノートブックは通常の掃除と同じ規則で retention_days 後に消える）。
"""

from __future__ import annotations

import copy
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import urldefrag, urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import radio_batch as rb  # noqa: E402

# 突き合わせの期間
WINDOW = datetime.timedelta(days=7)
# 直近 24 時間の記事は次の回で流れる途中なので「流していない」に数えない（1 日 1 回の実行を想定）
SETTLE = datetime.timedelta(hours=24)
# 通知に並べる件数
LIST_MAX = 8
# 記事ページの本文がこれより短ければ、JavaScript でしか中身を出さないページか確認ページとみなす
MIN_PAGE_TEXT = 400
# Deep Research の待ち時間（deep は 15〜30 分かかる）
RESEARCH_TIMEOUT = 1800


def norm(url):
    """照合用の URL。# 以降・クエリ・末尾の / を落とし、ホストは小文字にする。"""
    parsed = urlparse(urldefrag(url).url)
    return f"{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def site_of(url):
    """サイトの単位（blog.cloudflare.com と developers.cloudflare.com を同じ cloudflare.com とみなす）。"""
    host = urlparse(url).netloc.lower().split(':')[0]
    labels = host.split('.')
    return '.'.join(labels[-2:]) if len(labels) >= 2 else host


def recent_runs(state, now):
    runs = state.get(rb.RUNS_KEY)
    runs = runs if isinstance(runs, list) else []
    return [r for r in runs if now - datetime.datetime.fromisoformat(r['at']) <= WINDOW]


def reconcile(runs, results, now):
    """実行記録とフィードを突き合わせる。

    results は rb.check_rss_feeds の結果（state のコピーに対して回したもの）。RSS のフィードだけを見る:
    sitemap の日付は更新日なので「この 1 週間に出た記事」を決められない。
    流していない = 期間内（直近 24 時間を除く）に公開され、流した・見送った・取り込めなかったのどれでもなく、
    まだ未読で待っているのでもない記事。新しく足したフィードの初回分は既読化が仕様なので除く。
    """
    # 記録がある期間だけを見る。記録を取り始める前に流した記事を「流していない」と数えないため
    if not runs:
        return {'aired': set(), 'skipped': 0, 'duplicates': [], 'missed': [], 'pending': [], 'no_history': True}
    start = max(now - WINDOW, min(datetime.datetime.fromisoformat(r['at']) for r in runs))
    aired_per_run = [{norm(u) for u in r.get('aired', [])} for r in runs]
    aired = set().union(*aired_per_run)
    accounted = aired | {norm(u) for r in runs for u in [*r.get('skipped', []), *r.get('blocked', [])]}
    first_run_feeds = {name for r in runs for name in r.get('first_run', [])}
    twice = Counter(u for links in aired_per_run for u in links)

    missed, pending = [], []
    for result in results:
        if result.get('mode') != 'rss' or result['name'] in first_run_feeds or result.get('first_run'):
            continue
        threshold = rb.read_threshold(result['watermark'], result.get('floor'))
        recent = set(result['recent_ids'])
        for e in result['entries']:
            if e['ts'] is None or not (start <= e['ts'] <= now - SETTLE):
                continue
            if norm(e['link']) in accounted:
                continue
            if rb.is_unread(e['id'], e['ts'], threshold, recent):
                pending.append(e)
            else:
                missed.append(e)
    return {
        'aired': aired,
        'skipped': sum(len(r.get('skipped', [])) for r in runs),
        'duplicates': sorted(u for u, n in twice.items() if n >= 2),
        'missed': missed,
        'pending': pending,
        'no_history': False,
    }


def probe_article(url):
    """記事ページが NotebookLM に取れそうか。問題があれば説明、なければ None。"""
    try:
        response = httpx.get(url, timeout=rb.FEED_TIMEOUT, follow_redirects=True, headers={'User-Agent': rb.USER_AGENT})
    except Exception as e:
        return f'記事ページを取得できない（{type(e).__name__}）'
    if response.status_code >= 400:
        blocked = response.headers.get('cf-mitigated') == 'challenge'
        return f'記事ページが HTTP {response.status_code}' + ('（ボット対策の確認ページ）' if blocked else '')
    title = re.search(r'<title[^>]*>(.*?)</title>', response.text, re.S | re.I)
    if title and rb.JUNK_TITLE_RE.search(title.group(1)):
        return f'記事ページがボット対策の確認ページ（{title.group(1).strip()[:40]}）'
    if len(rb.strip_html(response.text)) < MIN_PAGE_TEXT:
        return '記事ページに本文がほとんど無い（JavaScript でしか中身を出さないページかもしれない）'
    return None


def check_articles(config, results):
    """URL で渡しているフィードごとに、最新記事のページが取れるかを見る。"""
    modes = {f['name']: f.get('source_mode', 'url') for f in config.get('feeds', [])}
    problems = []
    for result in results:
        if modes.get(result['name']) == rb.TEXT_SOURCE_MODE or not result['entries']:
            continue
        newest = max(result['entries'], key=lambda e: e['ts'] or rb.EPOCH)
        problem = probe_article(newest['link'])
        if problem:
            problems.append((result['name'], problem))
    return problems


def build_query(config, now):
    """Deep Research への問い。weekly_check.recall_query があればそれ（{start} {end} を日付に置き換える）。"""
    start, end = (now - WINDOW).date().isoformat(), now.date().isoformat()
    custom = (config.get('weekly_check') or {}).get('recall_query')
    if custom:
        return custom.replace('{start}', start).replace('{end}', end)
    names = ', '.join(sorted({f['name'] for f in config.get('feeds', [])}))
    return (
        f'Between {start} and {end}, what did these sources officially announce, release or change: {names}? '
        'List each item with its official URL (company blog, changelog, release notes or documentation). '
        'Prefer first-party sources over news coverage.'
    )


def classify_found(found, aired, feed_links, followed_sites):
    """Deep Research が見つけた URL を分ける。

    - aired: ラジオで流したもの
    - in_feeds: どこかのフィードに載っている（見送り・絞り込み・待ちのどれか）
    - gaps: 購読しているサイトの記事なのに、どのフィードにも載っていない（購読の穴）
    - elsewhere: 購読していないサイト（ニュースサイトや、まだ購読していない一次情報）
    """
    groups = {'aired': [], 'in_feeds': [], 'gaps': [], 'elsewhere': []}
    seen = set()
    for item in found:
        key = norm(item['url'])
        if not key or key in seen:
            continue
        seen.add(key)
        if key in aired:
            groups['aired'].append(item)
        elif key in feed_links:
            groups['in_feeds'].append(item)
        elif site_of(item['url']) in followed_sites:
            groups['gaps'].append(item)
        else:
            groups['elsewhere'].append(item)
    return groups


def _notebooklm(args, timeout):
    """notebooklm CLI を --json で呼ぶ。失敗しても出力の JSON（エラーの種類）を読めるよう、例外に載せて返す。"""
    cmd = ['notebooklm', *args, '--json']
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout, stderr=result.stderr)
    return json.loads(result.stdout)


def describe_recall_failure(e):
    """取りこぼし検査の失敗を短く説明する。CLI の出力からは既知のフィールド（種類と理由）だけを拾う。

    生の stdout/stderr はセッションクッキーを運びうるので載せない（rb.describe_failure と同じ方針）。
    """
    if isinstance(e, subprocess.TimeoutExpired):
        return f"notebooklm が {int(e.timeout)} 秒で終わらなかった"
    if not isinstance(e, subprocess.CalledProcessError):
        return type(e).__name__
    step = ' '.join(e.cmd[1:3])
    detail = rb._cli_error_envelope(e.output)
    if not detail:
        try:
            payload = json.loads(e.output or '')
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get('error'), str):
            detail = ': '.join(str(payload[k]) for k in ('status', 'error') if payload.get(k))
    return f"notebooklm {step} が終了コード {e.returncode} で失敗" + (f"（{detail}）" if detail else '')


def run_recall(config, now):
    """Deep Research を 1 回走らせ、見つかった [{url, title}] を返す。"""
    title = rb.notebook_title_for(config, topic=rb.RECALL_TOPIC, now=now)
    notebook_id = _notebooklm(['create', title], rb.NOTEBOOKLM_TIMEOUT)['notebook']['id']
    with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False, encoding='utf-8') as f:
        f.write(build_query(config, now))
        query_path = f.name
    try:
        _notebooklm(
            [
                'source',
                'add-research',
                '--mode',
                'deep',
                '--no-wait',
                '--notebook',
                notebook_id,
                '--prompt-file',
                query_path,
            ],
            rb.NOTEBOOKLM_TIMEOUT,
        )
    finally:
        os.unlink(query_path)
    result = _notebooklm(
        ['research', 'wait', '-n', notebook_id, '--timeout', str(RESEARCH_TIMEOUT)], RESEARCH_TIMEOUT + 300
    )
    found = [
        {'url': s['url'], 'title': s.get('title') or s['url']} for s in result.get('sources') or [] if s.get('url')
    ]
    # レポート本文の中のリンクも拾う（出典として引用されたページ）
    for url in re.findall(r'https?://[^\s)\]>"\'<]+', result.get('report') or ''):
        found.append({'url': url.rstrip('.,'), 'title': url})
    return found


def _link(url, title, is_discord):
    title = title.replace('\n', ' ').strip()[:70] or url
    return f"{title}\n  {url}" if is_discord else f"<{url}|{title}>"


def build_message(period, feed_total, warnings, article_problems, recon, recall, is_discord):
    """週次点検の通知文。問題がなければ短く、あるときだけ中身を並べる。"""
    if recon.get('no_history'):
        lines = [f"🗓 週次点検（{period}）", '突き合わせは、実行記録がたまる次回から']
        lines += _problem_lines(warnings, article_problems, recon, recall, is_discord)
        return '\n'.join(lines)
    lines = [
        f"🗓 週次点検（{period}）",
        f"流した {len(recon['aired'])} 本・鮮度で見送り {recon['skipped']} 本・二重 {len(recon['duplicates'])} 本・"
        f"流していない {len(recon['missed'])} 本・待ち {len(recon['pending'])} 本／"
        f"フィード {feed_total - len({n for n, _ in warnings if n})} / {feed_total} 本が問題なし",
    ]
    lines += _problem_lines(warnings, article_problems, recon, recall, is_discord)
    return '\n'.join(lines)


def _problem_lines(warnings, article_problems, recon, recall, is_discord):
    lines = []
    if warnings:
        lines.append('⚠️ フィードの警告')
        lines += [f"・{name}: {reason}" for name, reason in warnings[:LIST_MAX]]
    if article_problems:
        lines.append('⚠️ 記事ページ（source_mode: text を検討）')
        lines += [f"・{name}: {problem}" for name, problem in article_problems[:LIST_MAX]]
    if recon['missed']:
        lines.append('⚠️ フィードに載ったのに流していない記事（原因を調べる価値あり）')
        lines += [
            f"・{_link(e['link'], e['title'], is_discord)}（{e['feed_name']}）" for e in recon['missed'][:LIST_MAX]
        ]
    if recon['duplicates']:
        lines.append('⚠️ 二度流した記事')
        lines += [f"・{u}" for u in recon['duplicates'][:LIST_MAX]]
    if recall is not None:
        if isinstance(recall, str):
            lines.append(f"🔎 取りこぼし検査は今週は失敗した（{recall}）")
        else:
            followed = len(recall['aired']) + len(recall['in_feeds']) + len(recall['gaps'])
            lines.append(
                f"🔎 取りこぼし検査（Deep Research）: 購読中のサイトの記事 {followed} 件のうち、"
                f"流した {len(recall['aired'])}・フィードにある {len(recall['in_feeds'])}・どのフィードにも無い {len(recall['gaps'])}"
            )
            lines += [f"・{_link(i['url'], i['title'], is_discord)}" for i in recall['gaps'][:LIST_MAX]]
            others = Counter(site_of(i['url']) for i in recall['elsewhere'])
            if others:
                top = '、'.join(f"{site} {n}" for site, n in others.most_common(5))
                lines.append(f"購読していないサイト {len(recall['elsewhere'])} 件（{top}）")
    return lines


def main():
    webhook_url = os.environ.get('NOTIFY_WEBHOOK_URL')
    is_discord = bool(webhook_url and 'discord.com' in webhook_url)
    now = datetime.datetime.now(rb.UTC)
    period = f"{(now - WINDOW).astimezone(rb.LOCAL_TZ):%m/%d}〜{now.astimezone(rb.LOCAL_TZ):%m/%d}"
    try:
        config = rb.load_config()
        rb.validate_config(config)
        rb.apply_settings_overrides(config)
        period = f"{(now - WINDOW).astimezone(rb.LOCAL_TZ):%m/%d}〜{now.astimezone(rb.LOCAL_TZ):%m/%d}"
        state = rb.load_state()
        # 本体と同じ取得・判定を、state のコピーに対して回す（本物の state は変えない）
        _candidates, results, warnings = rb.check_rss_feeds(config, copy.deepcopy(state), now=now)
        recon = reconcile(recent_runs(state, now), results, now)
        article_problems = check_articles(config, results)

        recall = None
        if (config.get('weekly_check') or {}).get('recall'):
            if not os.environ.get('NOTEBOOKLM_AUTH_JSON'):
                recall = 'NOTEBOOKLM_AUTH_JSON が無い'
            else:
                try:
                    found = run_recall(config, now)
                    feed_links = {norm(e['link']) for r in results for e in r['entries']}
                    followed = {site_of(f['url']) for f in config['feeds']}
                    followed |= {site_of(e['link']) for r in results for e in r['entries']}
                    recall = classify_found(found, recon['aired'], feed_links, followed)
                except Exception as e:
                    print(f"Recall check failed: {rb.redact(e)}", file=sys.stderr)
                    recall = rb.redact(describe_recall_failure(e))

        text = build_message(period, len(config['feeds']), warnings, article_problems, recon, recall, is_discord)
        print(text)
        if webhook_url:
            rb._post_webhook(webhook_url, {'content': text} if is_discord else {'text': text}, 'Weekly check')
    except Exception as e:
        print(f"Weekly check failed: {rb.redact(e)}", file=sys.stderr)
        if webhook_url:
            text = f"🗓 週次点検が失敗した（{period}）: {rb.redact(rb.describe_failure(e))}"
            rb._post_webhook(webhook_url, {'content': text} if is_discord else {'text': text}, 'Weekly check failure')
        sys.exit(1)


if __name__ == '__main__':
    main()
