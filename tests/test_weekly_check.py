"""scripts/weekly_check.py の判定部分のテスト。ネットワークと notebooklm CLI には触れない。"""

import datetime
import json
import subprocess

import radio_batch as rb
from scripts import weekly_check as wc

UTC = datetime.UTC
NOW = datetime.datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
DAY = datetime.timedelta(days=1)


def entry(i, ts, link=None):
    return {
        'id': f'id-{i}',
        'ts': ts,
        'title': f'記事{i}',
        'link': link or f'https://news.example.com/{i}',
        'feed_name': 'F',
    }


def rss_result(entries, name='F', recent_ids=(), watermark=NOW - 10 * DAY, first_run=False):
    return {
        'name': name,
        'mode': 'rss',
        'entries': entries,
        'first_run': first_run,
        'watermark': watermark,
        'floor': NOW - 30 * DAY,
        'recent_ids': list(recent_ids),
    }


def test_norm_and_site():
    assert wc.norm('https://Blog.Example.com/post/?utm=x#top') == 'blog.example.com/post'
    assert wc.site_of('https://developers.cloudflare.com/changelog/') == 'cloudflare.com'
    assert wc.site_of('https://blog.google/products/gemini/') == 'blog.google'


def test_reconcile_classifies_each_article():
    aired = entry(1, NOW - 3 * DAY)
    skipped = entry(2, NOW - 3 * DAY)
    fresh = entry(3, NOW - 2 * datetime.timedelta(hours=6))  # 直近 24 時間: まだ数えない
    old = entry(4, NOW - 9 * DAY)  # 期間外
    pending = entry(5, NOW - 2 * DAY)  # 未読で待っている（既読の記録にない）
    lost = entry(6, NOW - 2 * DAY)  # 既読なのに流していない
    runs = [
        {'at': (NOW - 3 * DAY).isoformat(), 'aired': [aired['link']], 'skipped': [skipped['link']]},
        {'at': (NOW - 2 * DAY).isoformat(), 'aired': [aired['link'] + '/'], 'skipped': []},  # 二度目
    ]
    result = rss_result([aired, skipped, fresh, old, pending, lost], recent_ids=['id-6'])

    recon = wc.reconcile(runs, [result], NOW)

    assert [e['title'] for e in recon['missed']] == ['記事6']
    assert [e['title'] for e in recon['pending']] == ['記事5']
    assert recon['skipped'] == 1
    assert recon['duplicates'] == ['news.example.com/1']


def test_reconcile_ignores_new_feeds_blocked_articles_and_sitemaps():
    lost = entry(1, NOW - 2 * DAY)
    blocked = entry(2, NOW - 2 * DAY)
    runs = [{'at': (NOW - DAY).isoformat(), 'aired': [], 'blocked': [blocked['link']], 'first_run': ['New']}]
    results = [
        rss_result([lost], name='New', recent_ids=['id-1']),  # この週に足したフィードの初回分
        rss_result([blocked], recent_ids=['id-2']),  # ボット対策で取り込めなかった記事（別の通知で報告済み）
        {'name': 'S', 'mode': 'sitemap', 'entries': [lost]},
    ]
    recon = wc.reconcile(runs, results, NOW)
    assert recon['missed'] == [] and recon['pending'] == []


def test_reconcile_only_covers_the_recorded_period():
    """記録を取り始める前に流した記事を「流していない」と数えない（初回の週次点検）。"""
    before_history = entry(1, NOW - 5 * DAY)
    after_start = entry(2, NOW - 2 * DAY)
    runs = [{'at': (NOW - 3 * DAY).isoformat(), 'aired': []}]
    recon = wc.reconcile(runs, [rss_result([before_history, after_start], recent_ids=['id-1', 'id-2'])], NOW)
    assert [e['title'] for e in recon['missed']] == ['記事2']

    empty = wc.reconcile([], [rss_result([after_start], recent_ids=['id-2'])], NOW)
    assert empty['no_history'] and empty['missed'] == []
    text = wc.build_message('p', 20, [], [], empty, None, is_discord=False)
    assert '次回から' in text


def test_classify_found_separates_gaps_from_what_was_aired_or_is_in_feeds():
    found = [
        {'url': 'https://www.anthropic.com/news/aired#x', 'title': 'aired'},
        {'url': 'https://www.anthropic.com/news/in-feed/', 'title': 'in feed'},
        {'url': 'https://www.anthropic.com/claude-opus-9', 'title': 'gap'},
        {'url': 'https://techcrunch.com/story', 'title': 'news'},
        {'url': 'https://www.anthropic.com/claude-opus-9?ref=x', 'title': 'duplicate of gap'},
    ]
    groups = wc.classify_found(
        found,
        aired={'www.anthropic.com/news/aired'},
        feed_links={'www.anthropic.com/news/in-feed'},
        followed_sites={'anthropic.com'},
    )
    assert [i['title'] for i in groups['aired']] == ['aired']
    assert [i['title'] for i in groups['in_feeds']] == ['in feed']
    assert [i['title'] for i in groups['gaps']] == ['gap']
    assert [i['title'] for i in groups['elsewhere']] == ['news']


def test_build_query_default_and_custom():
    config = {'feeds': [{'name': 'Vercel', 'url': 'https://vercel.com/atom'}, {'name': 'Claude Blog', 'url': 'x'}]}
    query = wc.build_query(config, NOW)
    assert 'Claude Blog, Vercel' in query and '2026-07-13' in query and '2026-07-20' in query
    custom = {**config, 'weekly_check': {'recall_query': 'From {start} to {end}: what shipped?'}}
    assert wc.build_query(custom, NOW) == 'From 2026-07-13 to 2026-07-20: what shipped?'


def empty_recon():
    return {'aired': {'a', 'b'}, 'skipped': 0, 'duplicates': [], 'missed': [], 'pending': [], 'no_history': False}


def test_message_is_short_when_all_is_well():
    text = wc.build_message('07/13〜07/20', 20, [], [], empty_recon(), None, is_discord=False)
    assert text.count('\n') == 1
    assert '流した 2 本' in text and '20 / 20 本が問題なし' in text


def test_message_lists_problems_and_recall_gaps():
    recon = {**empty_recon(), 'missed': [entry(1, NOW - 2 * DAY)], 'duplicates': ['news.example.com/9']}
    recall = {
        'aired': [{'url': 'u1', 'title': 't'}],
        'in_feeds': [],
        'gaps': [{'url': 'https://www.anthropic.com/claude-opus-9', 'title': 'Opus 9'}],
        'elsewhere': [{'url': 'https://techcrunch.com/a', 'title': 'x'}],
    }
    text = wc.build_message(
        'p', 20, [('GitHub', 'HTTP 404')], [('OpenAI', '記事ページが HTTP 403')], recon, recall, False
    )
    for expected in (
        'GitHub: HTTP 404',
        'OpenAI: 記事ページが HTTP 403',
        '記事1',
        '二度流した',
        'Opus 9',
        'techcrunch.com 1',
    ):
        assert expected in text
    assert '19 / 20 本が問題なし' in text


def test_recall_failure_reports_only_known_fields():
    """CLI の出力からは既知のフィールドだけを拾う（生の stderr はクッキーを運びうる）。"""
    e = subprocess.CalledProcessError(
        1,
        ['notebooklm', 'research', 'wait', '-n', 'nb', '--json'],
        output=json.dumps({'status': 'timeout', 'error': 'Timed out after 1800s'}),
        stderr='cookie=SECRET',
    )
    text = wc.describe_recall_failure(e)
    assert 'research wait' in text and 'timeout: Timed out after 1800s' in text
    assert 'SECRET' not in text


def test_run_recall_collects_source_urls_and_report_links(monkeypatch):
    calls = []

    def fake(args, timeout):
        calls.append(args)
        if args[0] == 'create':
            return {'notebook': {'id': 'nb-1'}}
        if args[:2] == ['source', 'add-research']:
            return {'status': 'started'}
        return {
            'status': 'completed',
            'sources': [{'url': 'https://vercel.com/changelog/x', 'title': 'X'}, {'title': 'report only'}],
            'report': 'See [the post](https://blog.cloudflare.com/y/). Also https://github.blog/changelog/z.',
        }

    monkeypatch.setattr(wc, '_notebooklm', fake)
    config = {'feeds': [], 'settings': {'notebook_title_format': 'Tech Radio {topic} {date}'}}
    found = wc.run_recall(config, NOW)

    assert [i['url'] for i in found] == [
        'https://vercel.com/changelog/x',
        'https://blog.cloudflare.com/y/',
        'https://github.blog/changelog/z',
    ]
    assert calls[0] == ['create', f'Tech Radio {rb.RECALL_TOPIC} 2026-07-20']
    assert '--no-wait' in calls[1] and '--prompt-file' in calls[1]


def test_article_check_skips_text_mode_feeds(monkeypatch):
    probed = []
    monkeypatch.setattr(wc, 'probe_article', lambda url: probed.append(url) or 'HTTP 403')
    config = {'feeds': [{'name': 'OpenAI', 'url': 'o', 'source_mode': 'text'}, {'name': 'Vercel', 'url': 'v'}]}
    results = [
        rss_result([entry(1, NOW)], name='OpenAI'),
        rss_result([entry(2, NOW - DAY), entry(3, NOW)], name='Vercel'),
    ]
    assert wc.check_articles(config, results) == [('Vercel', 'HTTP 403')]
    assert probed == ['https://news.example.com/3']  # 最新の記事だけを見る
