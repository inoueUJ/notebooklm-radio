import datetime
import subprocess

import pytest

import radio_batch as rb

UTC = datetime.UTC
FEED_URL = 'https://example.com/feed'
CONFIG = {'feeds': [{'name': 'Example', 'url': FEED_URL}]}


class FakeEntry:
    def __init__(self, index, published=None):
        self.id = f'post-{index:05d}'
        self.title = f'記事{index}'
        self.link = f'https://example.com/{index}'
        if published is not None:
            self.published_parsed = published.timetuple()


class FakeFeed:
    def __init__(self, entries):
        self.entries = entries
        self.bozo = False


def make_feed(count, start=None, newest_first=True):
    """実フィードと同じく新しい順で返す。1件目が最古。"""
    start = start or datetime.datetime(2020, 1, 1, tzinfo=UTC)
    entries = [FakeEntry(i, start + datetime.timedelta(hours=i)) for i in range(1, count + 1)]
    return FakeFeed(list(reversed(entries)) if newest_first else entries)


@pytest.fixture
def feed_box(monkeypatch):
    """fetch_feed を差し替え、テスト側からフィードの中身を入れ替えられるようにする。"""
    box = {'feed': make_feed(10), 'problem': None}
    monkeypatch.setattr(rb, 'fetch_feed', lambda url: (box['feed'], box['problem']))
    return box


def run_once(state, feed_box=None):
    """check_rss_feeds → select → advance_state という本番と同じ流れを1回まわす。"""
    candidates, results, warnings = rb.check_rss_feeds(CONFIG, state)
    selected = rb.select_articles(candidates) if candidates else []
    rb.advance_state(state, results, selected)
    return [a['title'] for a in selected], warnings


# --- 障害1 のリグレッションテスト -------------------------------------------


def test_second_run_detects_nothing_new(feed_box):
    """2回連続で実行したら、2回目は新着ゼロでなければならない。"""
    state = {}
    run_once(state, feed_box)
    titles, _ = run_once(state, feed_box)
    assert titles == []


def test_large_feed_does_not_oscillate(feed_box):
    """1325件(Vercel実測値)のフィードでも、既読が未読に戻ってはいけない。"""
    feed_box['feed'] = make_feed(1325)
    state = {}

    first, _ = run_once(state, feed_box)
    assert first == ['記事1325']

    # 新着がない限り、何度まわしても検知はゼロ
    for _ in range(5):
        titles, _ = run_once(state, feed_box)
        assert titles == [], f'既読の記事が未読に戻った: {titles}'


def test_large_feed_detects_only_genuinely_new(feed_box):
    """1325件のフィードに1件追加したら、その1件だけが検知される。"""
    feed_box['feed'] = make_feed(1325)
    state = {}
    run_once(state, feed_box)

    feed_box['feed'] = make_feed(1326)
    titles, _ = run_once(state, feed_box)
    assert titles == ['記事1326']


def test_recent_ids_stays_bounded(feed_box):
    """state が無限に肥大化しない。"""
    feed_box['feed'] = make_feed(1325)
    state = {}
    run_once(state, feed_box)
    assert len(state[FEED_URL]['recent_ids']) <= rb.MAX_RECENT_IDS


# --- 上限とデータロス --------------------------------------------------------


def test_first_run_processes_only_latest(feed_box):
    feed_box['feed'] = make_feed(50)
    state = {}
    titles, _ = run_once(state, feed_box)
    assert titles == ['記事50']


def test_overflow_is_carried_over_not_dropped(feed_box):
    """上限を超えた新着は「既読扱いで捨てる」のではなく、次回に持ち越す。"""
    feed_box['feed'] = make_feed(10)
    state = {}
    run_once(state, feed_box)  # 初回: 記事10 のみ処理、記事1-9 は既読

    # 新たに 9 件公開される
    feed_box['feed'] = make_feed(19)

    seen = []
    for _ in range(5):
        titles, _ = run_once(state, feed_box)
        seen.extend(titles)

    # 9件すべてが、古い順に、重複なく処理される
    assert seen == [f'記事{i}' for i in range(11, 20)]


def test_per_feed_cap_is_respected(feed_box):
    feed_box['feed'] = make_feed(10)
    state = {}
    run_once(state, feed_box)
    feed_box['feed'] = make_feed(30)
    titles, _ = run_once(state, feed_box)
    assert len(titles) == rb.MAX_ARTICLES_PER_FEED


# --- 公開時刻を持たないエントリ ----------------------------------------------


def test_dateless_entries_are_not_reprocessed(feed_box):
    dateless = [FakeEntry(1), FakeEntry(2)]
    feed_box['feed'] = FakeFeed(dateless)

    state = {}
    first, _ = run_once(state, feed_box)
    assert len(first) == 1

    second, _ = run_once(state, feed_box)
    assert second == []


# --- フィードの健全性 --------------------------------------------------------


def test_dead_feed_warns_and_stays_first_run(feed_box):
    feed_box['feed'] = None
    feed_box['problem'] = 'HTTP 403'

    state = {}
    titles, warnings = run_once(state, feed_box)
    assert titles == []
    assert warnings == [('Example', 'HTTP 403')]
    # 死んだフィードで state を初期化してしまわない
    assert FEED_URL not in state


def test_recovered_feed_runs_as_first_run(feed_box):
    feed_box['feed'] = None
    feed_box['problem'] = 'エントリが0件'
    state = {}
    run_once(state, feed_box)

    feed_box['feed'] = make_feed(5)
    feed_box['problem'] = None
    titles, _ = run_once(state, feed_box)
    assert titles == ['記事5']


# --- 旧形式 state からの移行 -------------------------------------------------


def test_legacy_list_state_is_migrated(feed_box):
    feed_box['feed'] = make_feed(20)
    state = {FEED_URL: ['post-00019', 'post-00020']}
    titles, _ = run_once(state, feed_box)

    assert titles == ['記事20']  # 初回実行として扱われる
    assert isinstance(state[FEED_URL], dict)
    assert 'watermark' in state[FEED_URL]


# --- タイムゾーン ------------------------------------------------------------


def test_notebook_title_uses_jst_not_utc():
    config = {'settings': {'notebook_title_format': 'Tech Radio {date}'}}
    # 07:00 JST 7/10 == 22:00 UTC 7/9。UTC 日付を使うと前日名になる。
    utc_now = datetime.datetime(2026, 7, 9, 22, 0, tzinfo=UTC)
    assert rb.notebook_title_for(config, now=utc_now) == 'Tech Radio 2026-07-10'


def test_notebook_title_includes_topic():
    config = {'settings': {'notebook_title_format': 'Tech Radio {topic} {date}'}}
    now = datetime.datetime(2026, 7, 10, 9, 0, tzinfo=rb.JST)
    assert rb.notebook_title_for(config, topic='Infra', now=now) == 'Tech Radio Infra 2026-07-10'
    assert rb.notebook_title_for(config, now=now) == f'Tech Radio {rb.DEFAULT_TOPIC} 2026-07-10'


# --- 全体上限 ----------------------------------------------------------------


def test_select_articles_keeps_oldest_and_all_first_runs():
    def art(i, first_run=False):
        return {
            'id': f'a{i}',
            'ts': datetime.datetime(2026, 1, 1, tzinfo=UTC) + datetime.timedelta(hours=i),
            'title': f't{i}',
            'link': f'l{i}',
            'feed_name': 'F',
            'feed_url': 'u',
            '_first_run': first_run,
        }

    candidates = [art(i) for i in range(20)] + [art(99, first_run=True)]
    selected = rb.select_articles(candidates)

    assert len(selected) == rb.MAX_ARTICLES_TOTAL
    assert selected[0]['title'] == 't99'  # 初回分は必ず残る
    # 残りは古い順
    rest = [a['title'] for a in selected[1:]]
    assert rest == [f't{i}' for i in range(rb.MAX_ARTICLES_TOTAL - 1)]


# --- Secret のマスキング -----------------------------------------------------


def test_redact_removes_secrets(monkeypatch):
    monkeypatch.setenv('NOTIFY_WEBHOOK_URL', 'https://hooks.slack.com/services/SUPERSECRET')
    monkeypatch.setenv('NOTEBOOKLM_AUTH_JSON', '{"cookies": [{"value": "abcdefghijklmnopqrstuvwxyz"}]}')

    text = 'failed: https://hooks.slack.com/services/SUPERSECRET cookie=abcdefghijklmnopqrstuvwxyz'
    out = rb.redact(text)

    assert 'SUPERSECRET' not in out
    assert 'abcdefghijklmnopqrstuvwxyz' not in out


def test_redact_truncates():
    assert len(rb.redact('x' * 5000)) == 1500


# --- sitemap フィード ---------------------------------------------------------

SITEMAP_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/news</loc><lastmod>2026-07-01T00:00:00.000Z</lastmod></url>
  <url><loc>https://example.com/news/hello-world</loc><lastmod>2026-07-02T09:30:00.000Z</lastmod></url>
  <url><loc>https://example.com/news/no-date</loc></url>
  <url><loc>https://example.com/about</loc><lastmod>2026-07-03T00:00:00.000Z</lastmod></url>
</urlset>
"""


class FakeResponse:
    def __init__(self, content=b'', status_code=200):
        self.content = content
        self.status_code = status_code


def test_sitemap_feed_filters_by_prefix_and_reads_lastmod(monkeypatch):
    monkeypatch.setattr(rb.httpx, 'get', lambda *a, **k: FakeResponse(SITEMAP_XML))
    feed, problem = rb.fetch_sitemap_feed('https://example.com/sitemap.xml', 'https://example.com/news/')

    assert problem is None
    # /news 自身と /about は除外され、記事2件だけが残る
    assert [e.link for e in feed.entries] == [
        'https://example.com/news/hello-world',
        'https://example.com/news/no-date',
    ]
    dated, dateless = feed.entries
    assert dated.title == 'hello world'
    assert rb.entry_time(dated) == datetime.datetime(2026, 7, 2, 9, 30, tzinfo=UTC)
    assert rb.entry_time(dateless) is None


def test_sitemap_feed_reports_problems(monkeypatch):
    monkeypatch.setattr(rb.httpx, 'get', lambda *a, **k: FakeResponse(status_code=404))
    feed, problem = rb.fetch_sitemap_feed('https://example.com/sitemap.xml', 'https://example.com/news/')
    assert feed is None and problem == 'HTTP 404'

    monkeypatch.setattr(rb.httpx, 'get', lambda *a, **k: FakeResponse(SITEMAP_XML))
    feed, problem = rb.fetch_sitemap_feed('https://example.com/sitemap.xml', 'https://example.com/nothing/')
    assert feed is None and 'prefix' in problem


def test_sitemap_feed_end_to_end_detects_new_article(monkeypatch):
    """sitemap型フィードでも 初回→新着なし→新着1件 の流れが watermark で回る。"""
    config = {
        'feeds': [
            {
                'name': 'Sitemap',
                'type': 'sitemap',
                'url': 'https://example.com/sitemap.xml',
                'prefix': 'https://example.com/news/',
            }
        ]
    }
    box = {'content': SITEMAP_XML}
    monkeypatch.setattr(rb.httpx, 'get', lambda *a, **k: FakeResponse(box['content']))

    def run(state):
        candidates, results, _ = rb.check_rss_feeds(config, state)
        rb.advance_state(state, results, candidates)
        return [a['link'] for a in candidates]

    state = {}
    assert run(state) == ['https://example.com/news/hello-world']  # 初回は最新1件
    assert run(state) == []  # 変化なし

    box['content'] = SITEMAP_XML.replace(
        b'</urlset>',
        b'<url><loc>https://example.com/news/brand-new</loc><lastmod>2026-07-05T00:00:00.000Z</lastmod></url></urlset>',
    )
    assert run(state) == ['https://example.com/news/brand-new']


SITEMAP_CONFIG = {
    'feeds': [
        {
            'name': 'Sitemap',
            'type': 'sitemap',
            'url': 'https://example.com/sitemap.xml',
            'prefix': 'https://example.com/news/',
        }
    ]
}


def sitemap_run(config, state):
    candidates, results, _ = rb.check_rss_feeds(config, state)
    rb.advance_state(state, results, candidates)
    return [a['link'] for a in candidates]


def test_sitemap_lastmod_bump_does_not_resurface_article(monkeypatch):
    """既存記事の lastmod だけ動いても新着扱いしない（4月の記事が7月に「新着」で再浮上した障害の再発防止）。"""
    box = {'content': SITEMAP_XML}
    monkeypatch.setattr(rb.httpx, 'get', lambda *a, **k: FakeResponse(box['content']))

    state = {}
    sitemap_run(SITEMAP_CONFIG, state)  # 初回: 全既読化

    # 既存記事が編集されて lastmod が最近の日付に動く
    box['content'] = SITEMAP_XML.replace(b'2026-07-02T09:30:00.000Z', b'2026-07-14T00:00:00.000Z')
    assert sitemap_run(SITEMAP_CONFIG, state) == []


SHARED_SITEMAP = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/news/first</loc><lastmod>2026-07-10T00:00:00Z</lastmod></url>
  <url><loc>https://example.com/eng/intro</loc><lastmod>2026-07-01T00:00:00Z</lastmod></url>
</urlset>
"""

SHARED_CONFIG = {
    'feeds': [
        {
            'name': 'News',
            'type': 'sitemap',
            'url': 'https://example.com/sitemap.xml',
            'prefix': 'https://example.com/news/',
        },
        {
            'name': 'Eng',
            'type': 'sitemap',
            'url': 'https://example.com/sitemap.xml',
            'prefix': 'https://example.com/eng/',
        },
    ]
}


def test_sitemap_feeds_sharing_url_have_independent_state(monkeypatch):
    """同一 sitemap を prefix 違いで購読する2フィードが state を奪い合わない（Anthropic news/engineering の障害）。"""
    box = {'content': SHARED_SITEMAP}
    monkeypatch.setattr(rb.httpx, 'get', lambda *a, **k: FakeResponse(box['content']))

    state = {}
    assert sorted(sitemap_run(SHARED_CONFIG, state)) == [
        'https://example.com/eng/intro',
        'https://example.com/news/first',
    ]
    # state はフィードごとに独立したキーで持つ
    assert 'sitemap:https://example.com/sitemap.xml:https://example.com/news/' in state
    assert 'sitemap:https://example.com/sitemap.xml:https://example.com/eng/' in state

    # news 側の最新(7/10)より古い lastmod の eng 記事(7/5)でも、確実に検知される
    box['content'] = SHARED_SITEMAP.replace(
        b'</urlset>',
        b'<url><loc>https://example.com/eng/older-than-news</loc><lastmod>2026-07-05T00:00:00Z</lastmod></url></urlset>',
    )
    assert sitemap_run(SHARED_CONFIG, state) == ['https://example.com/eng/older-than-news']


def test_sitemap_legacy_watermark_state_is_migrated(monkeypatch):
    """旧形式（URLキー + watermark）の state から既読集合へ移行し、再処理も取りこぼしもしない。"""
    box = {'content': SITEMAP_XML}
    monkeypatch.setattr(rb.httpx, 'get', lambda *a, **k: FakeResponse(box['content']))

    state = {
        'https://example.com/sitemap.xml': {
            'watermark': '2026-07-02T09:30:00+00:00',
            'recent_ids': ['https://example.com/news/no-date'],
            'last_new': '2026-07-02T10:00:00+00:00',
        }
    }
    # 移行直後: 既読は既読のまま（初回扱いで最新1件が再処理されたりしない）
    assert sitemap_run(SITEMAP_CONFIG, state) == []
    key = 'sitemap:https://example.com/sitemap.xml:https://example.com/news/'
    assert 'https://example.com/sitemap.xml' not in state  # 旧キーは掃除される
    assert set(state[key]['seen']) == {
        'https://example.com/news/hello-world',
        'https://example.com/news/no-date',
    }
    assert state[key]['last_new'] == '2026-07-02T10:00:00+00:00'  # 停滞検知用の日時も引き継ぐ

    # 移行後も新着は普通に検知される
    box['content'] = SITEMAP_XML.replace(
        b'</urlset>',
        b'<url><loc>https://example.com/news/post-migration</loc></url></urlset>',
    )
    assert sitemap_run(SITEMAP_CONFIG, state) == ['https://example.com/news/post-migration']


# --- ページ更新監視 (watch) ---------------------------------------------------

WATCH_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://support.example.com/en/articles/111-widget-promo</loc><lastmod>2026-07-01T00:00:00Z</lastmod></url>
  <url><loc>https://support.example.com/en/articles/222-usage-limits</loc><lastmod>2026-07-02T00:00:00Z</lastmod></url>
  <url><loc>https://support.example.com/en/articles/333-how-to-login</loc><lastmod>2026-07-03T00:00:00Z</lastmod></url>
</urlset>
"""

WATCH_CONFIG = {
    'watch': [
        {
            'name': 'Support',
            'url': 'https://support.example.com/sitemap.xml',
            'prefix': 'https://support.example.com/en/articles/',
            'keywords': ['widget', 'usage'],
        }
    ]
}


@pytest.fixture
def watch_box(monkeypatch):
    box = {'content': WATCH_XML, 'posted': []}
    monkeypatch.setattr(rb.httpx, 'get', lambda *a, **k: FakeResponse(box['content']))
    monkeypatch.setattr(rb, '_post_webhook', lambda url, payload, label: box['posted'].append(payload))
    return box


def test_watch_first_run_records_without_notifying(watch_box):
    state = {}
    warnings = rb.check_watch_pages(WATCH_CONFIG, state, 'https://hooks.example.com')
    assert warnings == []
    assert watch_box['posted'] == []
    # キーワードに一致した2件だけ記録される（how-to-login は対象外）
    key = 'watch:https://support.example.com/sitemap.xml:https://support.example.com/en/articles/'
    assert set(state[key]['lastmod']) == {
        'https://support.example.com/en/articles/111-widget-promo',
        'https://support.example.com/en/articles/222-usage-limits',
    }


def test_watch_detects_update_and_new_page(watch_box):
    state = {}
    rb.check_watch_pages(WATCH_CONFIG, state, 'https://hooks.example.com')

    # 変化なし → 通知なし
    rb.check_watch_pages(WATCH_CONFIG, state, 'https://hooks.example.com')
    assert watch_box['posted'] == []

    # 既存記事の lastmod が動き、新記事も増える（キーワード非該当の新記事は無視）
    watch_box['content'] = WATCH_XML.replace(b'2026-07-01T00:00:00Z', b'2026-07-10T00:00:00Z').replace(
        b'</urlset>',
        b'<url><loc>https://support.example.com/en/articles/444-widget-pricing</loc>'
        b'<lastmod>2026-07-10T00:00:00Z</lastmod></url>'
        b'<url><loc>https://support.example.com/en/articles/555-irrelevant</loc>'
        b'<lastmod>2026-07-10T00:00:00Z</lastmod></url></urlset>',
    )
    rb.check_watch_pages(WATCH_CONFIG, state, 'https://hooks.example.com')
    assert len(watch_box['posted']) == 1
    text = watch_box['posted'][0]['text']
    assert '更新' in text and '111-widget-promo' in text
    assert '新規' in text and '444-widget-pricing' in text
    assert '555-irrelevant' not in text

    # 通知後は既読になり、再通知されない
    rb.check_watch_pages(WATCH_CONFIG, state, 'https://hooks.example.com')
    assert len(watch_box['posted']) == 1


def test_watch_fetch_failure_does_not_initialize_state(watch_box, monkeypatch):
    monkeypatch.setattr(rb.httpx, 'get', lambda *a, **k: FakeResponse(status_code=404))
    state = {}
    warnings = rb.check_watch_pages(WATCH_CONFIG, state, 'https://hooks.example.com')
    assert warnings == [('Support', 'HTTP 404')]
    assert state == {}  # 一時的な障害で初期化されない → 次回が初回として扱われる


# --- メンテナンス（削除・停滞検知） -------------------------------------------

CLEANUP_CONFIG = {
    'settings': {
        'notebook_title_format': 'Tech Radio {date}',
        'cleanup': {'enabled': True, 'retention_days': 7, 'dry_run': False},
    }
}

NOTEBOOKS = [
    {'id': 'a', 'title': 'Tech Radio 2026-07-01'},  # 11日前 → 削除対象
    {'id': 'b', 'title': 'Tech Radio 2026-07-10'},  # 2日前 → 保持
    {'id': 'c', 'title': '手動で作った大事なノート'},  # 対象外
    {'id': 'd', 'title': 'Tech Radio メモ'},  # 日付形式でない → 対象外
    {'id': 'e', 'title': 'XTech Radio 2026-01-01Y'},  # 完全一致でない → 対象外
]

NOW = datetime.datetime(2026, 7, 12, 9, 0, tzinfo=rb.JST)


@pytest.fixture
def notebooklm_box(monkeypatch):
    box = {'notebooks': NOTEBOOKS, 'deleted': [], 'posted': []}

    def fake_cli(args, retries=1):
        if args[0] == 'list':
            return {'notebooks': box['notebooks']}
        if args[0] == 'delete':
            box['deleted'].append(args[2])
            return {}
        raise AssertionError(f'unexpected CLI call: {args}')

    monkeypatch.setattr(rb, 'run_notebooklm_json', fake_cli)
    monkeypatch.setattr(rb, '_post_webhook', lambda url, payload, label: box['posted'].append(payload))
    return box


def test_cleanup_deletes_only_old_batch_notebooks(notebooklm_box):
    rb.cleanup_old_notebooks(CLEANUP_CONFIG, 'https://hooks.slack.com/x', now=NOW)
    # 削除されるのは「バッチ命名かつ7日より古い」1件だけ。手動ノートには触れない
    assert notebooklm_box['deleted'] == ['a']
    assert 'Tech Radio 2026-07-01' in notebooklm_box['posted'][0]['text']


def test_cleanup_matches_topic_titles_and_legacy_format(notebooklm_box):
    """トピック入りの命名と分割前の旧命名は削除対象。設定にないトピック名は対象外のまま。"""
    config = {
        'feeds': [{'name': 'A', 'url': 'u1', 'topic': 'AI'}, {'name': 'B', 'url': 'u2', 'topic': 'Infra'}],
        'settings': {
            'notebook_title_format': 'Tech Radio {topic} {date}',
            'cleanup': {
                'enabled': True,
                'retention_days': 7,
                'dry_run': False,
                'legacy_title_formats': ['Tech Radio {date}'],
            },
        },
    }
    notebooklm_box['notebooks'] = [
        {'id': 'a', 'title': 'Tech Radio AI 2026-07-01'},  # 古い + 現行命名 → 削除
        {'id': 'b', 'title': 'Tech Radio Infra 2026-07-01'},  # 古い + 現行命名 → 削除
        {'id': 'c', 'title': 'Tech Radio 2026-07-01'},  # 古い + 旧命名 → 削除
        {'id': 'd', 'title': 'Tech Radio AI 2026-07-11'},  # 新しい → 保持
        {'id': 'e', 'title': 'Tech Radio Podcast 2026-07-01'},  # 設定にないトピック名 → 触らない
    ]
    rb.cleanup_old_notebooks(config, 'https://hooks.slack.com/x', now=NOW)
    assert notebooklm_box['deleted'] == ['a', 'b', 'c']


def test_cleanup_dry_run_deletes_nothing(notebooklm_box):
    config = {'settings': {**CLEANUP_CONFIG['settings'], 'cleanup': {'enabled': True, 'dry_run': True}}}
    rb.cleanup_old_notebooks(config, 'https://hooks.slack.com/x', now=NOW)
    assert notebooklm_box['deleted'] == []
    assert 'ドライラン' in notebooklm_box['posted'][0]['text']


def test_cleanup_disabled_does_nothing(notebooklm_box):
    rb.cleanup_old_notebooks({'settings': {}}, 'https://hooks.slack.com/x', now=NOW)
    assert notebooklm_box['deleted'] == [] and notebooklm_box['posted'] == []


def test_stale_feed_notified_only_on_first_of_month(notebooklm_box):
    config = {'feeds': [{'name': 'Dead', 'url': FEED_URL}], 'settings': {}}
    # 実時刻を基準にすると「1日時点での経過日数」が日々縮み、いつか閾値を割ってテストが落ちる。
    # 判定される日(first)から遡って作ること。
    first = datetime.datetime(2026, 8, 1, 9, 0, tzinfo=rb.JST)
    old = (first - datetime.timedelta(days=40)).astimezone(UTC).isoformat()
    state = {FEED_URL: {'watermark': old, 'recent_ids': [], 'last_new': old}}

    rb.check_stale_feeds(config, state, 'https://hooks.slack.com/x', now=NOW)  # 12日 → 通知しない
    assert notebooklm_box['posted'] == []

    rb.check_stale_feeds(config, state, 'https://hooks.slack.com/x', now=first)
    assert 'Dead' in notebooklm_box['posted'][0]['text']


def test_dateless_only_feed_is_not_reprocessed(feed_box):
    """日付なしフィード(claude.comのsitemap等)でも、処理済み記事が翌日再処理されない。"""
    feed_box['feed'] = rb.SimpleNamespace(entries=[FakeEntry(i) for i in range(1, 4)])
    state = {}
    run_once(state, feed_box)  # 初回: 全既読化

    feed_box['feed'].entries.append(FakeEntry(4))  # 新着(日付なし)
    assert run_once(state, feed_box)[0] == ['記事4']
    assert run_once(state, feed_box)[0] == []  # 翌日、同じ記事が再処理されないこと


# --- 通知と音声プロンプト -----------------------------------------------------


def test_notification_includes_article_urls(monkeypatch):
    posted = []
    monkeypatch.setattr(rb, '_post_webhook', lambda url, payload, label: posted.append(payload))
    articles = [{'title': '新機能', 'link': 'https://example.com/post', 'feed_name': 'Example'}]
    rb.send_notification('https://hooks.slack.com/x', articles)
    assert 'https://example.com/post' in posted[0]['text']


def test_notification_groups_by_topic_and_reports_blocked(monkeypatch):
    posted = []
    monkeypatch.setattr(rb, '_post_webhook', lambda url, payload, label: posted.append(payload))
    articles = [
        {'title': 'A', 'link': 'https://a.example/1', 'feed_name': 'F1', 'topic': 'AI'},
        {'title': 'B', 'link': 'https://b.example/2', 'feed_name': 'F2', 'topic': 'Infra'},
    ]
    blocked = [('https://openai.example/3', 'Just a moment...')]
    rb.send_notification('https://hooks.slack.com/x', articles, blocked)

    text = posted[0]['text']
    assert '《AI》' in text and '《Infra》' in text
    assert 'ボット対策' in text and 'https://openai.example/3' in text


def test_notification_with_only_blocked_articles(monkeypatch):
    """全記事がボット対策ページだった場合も、「生成開始」とは言わずに警告だけ送る。"""
    posted = []
    monkeypatch.setattr(rb, '_post_webhook', lambda url, payload, label: posted.append(payload))
    rb.send_notification('https://hooks.slack.com/x', [], [('https://openai.example/3', 'Just a moment...')])

    text = posted[0]['text']
    assert '生成を開始' not in text
    assert 'ボット対策' in text


# --- ボット対策ページ（Just a moment...）の検知 --------------------------------


def make_source_cli(box):
    def fake_cli(args, retries=1):
        box['calls'].append(args)
        if args[:2] == ['source', 'add']:
            # URL投入は args[2] がURL、テキスト投入は本文なので --title から引く
            if '--type' in args:
                title = next(a.split('=', 1)[1] for a in args if a.startswith('--title='))
                return {'source': {'id': box['ids'][title]}}
            return {'source': {'id': box['ids'][args[2]]}}
        if args[:2] == ['source', 'list']:
            return {'sources': box['sources']}
        if args[:2] == ['source', 'delete']:
            return {}
        raise AssertionError(f'unexpected CLI call: {args}')

    return fake_cli


def article(link, title='記事', feed_name='Example', **kw):
    return {'title': title, 'link': link, 'feed_name': feed_name, **kw}


def test_junk_sources_are_removed_and_reported(monkeypatch):
    box = {
        'calls': [],
        'ids': {'https://good.example/a': 'src-ok', 'https://openai.example/b': 'src-junk'},
        'sources': [
            {'id': 'src-ok', 'title': '良い記事のタイトル'},
            {'id': 'src-junk', 'title': 'Just a moment...'},
        ],
    }
    monkeypatch.setattr(rb, 'run_notebooklm_json', make_source_cli(box))
    monkeypatch.setattr(rb.subprocess, 'run', lambda *a, **k: None)  # source wait は常に成功扱い

    audio_ok, blocked = rb.add_sources_and_wait('nb1', [article(u) for u in box['ids']])

    assert audio_ok is True  # 使えるソースが残っているので音声は生成する
    assert blocked == [('https://openai.example/b', 'Just a moment...')]
    # -y が無いと CI(TTY なし)で確認プロンプトが Abort になり、削除が成立しない
    assert ['source', 'delete', 'src-junk', '-y', '--notebook', 'nb1'] in box['calls']


def test_every_destructive_cli_call_skips_confirmation():
    """削除系は必ず -y を渡す。付け忘れると CI では黙って削除に失敗する。"""
    import inspect

    src = inspect.getsource(rb)
    for line in src.splitlines():
        stripped = line.strip()
        if "'delete'" not in stripped or stripped.startswith('#'):
            continue
        assert "'-y'" in stripped, f"削除呼び出しに -y がない: {stripped}"


def test_all_junk_sources_skip_audio(monkeypatch):
    box = {
        'calls': [],
        'ids': {'https://openai.example/b': 'src-junk'},
        'sources': [{'id': 'src-junk', 'title': 'Attention Required! | Cloudflare'}],
    }
    monkeypatch.setattr(rb, 'run_notebooklm_json', make_source_cli(box))
    monkeypatch.setattr(rb.subprocess, 'run', lambda *a, **k: None)

    audio_ok, blocked = rb.add_sources_and_wait('nb1', [article(u) for u in box['ids']])

    assert audio_ok is False  # 中身のあるソースがゼロなら音声を生成しない
    assert len(blocked) == 1


# --- テキスト投入モード (source_mode: text) -----------------------------------
#
# openai.com は Cloudflare のボット判定で記事ページが 403 を返し、NotebookLM が
# 検証ページを掴む。記事URLではなくRSSの配信内容を投入して迂回する経路のテスト。


TEXT_ARTICLE = {
    'title': 'Launching Health in ChatGPT',
    'link': 'https://openai.com/index/health-in-chatgpt',
    'feed_name': 'OpenAI',
    'topic': 'AI',
    'source_mode': 'text',
    'summary': 'Health in ChatGPT now lets eligible U.S. users connect medical records.',
    'ts': datetime.datetime(2026, 7, 23, 0, 0, tzinfo=UTC),
}


def test_text_source_add_does_not_send_the_url(monkeypatch):
    """テキスト投入では記事URLをCLIに渡さない（渡すとフェッチャーが検証ページを掴む）。"""
    box = {'calls': [], 'ids': {'Launching Health in ChatGPT': 'src-text'}, 'sources': []}
    monkeypatch.setattr(rb, 'run_notebooklm_json', make_source_cli(box))
    monkeypatch.setattr(rb.subprocess, 'run', lambda *a, **k: None)

    audio_ok, blocked = rb.add_sources_and_wait('nb1', [TEXT_ARTICLE])

    add_call = next(c for c in box['calls'] if c[:2] == ['source', 'add'])
    assert '--type' in add_call and add_call[add_call.index('--type') + 1] == 'text'
    assert add_call[2] != TEXT_ARTICLE['link']  # 位置引数は本文であってURLではない
    assert add_call[2].startswith('# ')
    assert audio_ok is True and blocked == []


def test_text_source_body_states_it_is_only_a_summary():
    """ラジオが「全文を読んだ」前提で喋らないよう、要約である旨を本文冒頭に必ず入れる。"""
    body = rb.build_text_source(TEXT_ARTICLE)

    assert '記事の全文ではありません' in body
    assert '推測で補わず' in body
    assert TEXT_ARTICLE['summary'] in body
    assert TEXT_ARTICLE['link'] in body  # 出典は辿れるように残す
    assert '2026-07-23' in body


def test_text_source_without_summary_says_title_only():
    """RSSに要約すらない場合、「要約がある」ふりをしない。"""
    body = rb.build_text_source({**TEXT_ARTICLE, 'summary': ''})
    assert 'タイトルと公開日しか判明していません' in body


def test_text_source_is_exempt_from_junk_detection(monkeypatch):
    """こちらで付けたタイトルをボット対策ページと誤検知して消さない。"""
    box = {
        'calls': [],
        'ids': {'Just a moment in AI history': 'src-text'},
        'sources': [{'id': 'src-text', 'title': 'Just a moment in AI history'}],
    }
    monkeypatch.setattr(rb, 'run_notebooklm_json', make_source_cli(box))
    monkeypatch.setattr(rb.subprocess, 'run', lambda *a, **k: None)

    audio_ok, blocked = rb.add_sources_and_wait('nb1', [{**TEXT_ARTICLE, 'title': 'Just a moment in AI history'}])

    assert blocked == []
    assert audio_ok is True
    assert not [c for c in box['calls'] if c[:2] == ['source', 'delete']]


def test_text_and_url_sources_mix_in_one_notebook(monkeypatch):
    """テキスト投入のフィードを足しても、他フィードのURL投入の挙動は変わらない。"""
    box = {
        'calls': [],
        'ids': {'https://good.example/a': 'src-url', 'Launching Health in ChatGPT': 'src-text'},
        'sources': [{'id': 'src-url', 'title': '良い記事'}, {'id': 'src-text', 'title': 'Launching Health in ChatGPT'}],
    }
    monkeypatch.setattr(rb, 'run_notebooklm_json', make_source_cli(box))
    monkeypatch.setattr(rb.subprocess, 'run', lambda *a, **k: None)

    audio_ok, blocked = rb.add_sources_and_wait('nb1', [article('https://good.example/a'), TEXT_ARTICLE])

    adds = [c for c in box['calls'] if c[:2] == ['source', 'add']]
    assert adds[0][2] == 'https://good.example/a' and '--type' not in adds[0]
    assert '--type' in adds[1]
    assert audio_ok is True and blocked == []


def test_title_starting_with_dash_is_not_parsed_as_an_option():
    args = rb.source_add_args({**TEXT_ARTICLE, 'title': '--force とは何か'}, 'nb1')
    assert '--title=--force とは何か' in args


def test_feed_source_mode_and_summary_reach_the_article(feed_box):
    """config の source_mode と RSS の要約が記事dictまで運ばれる。"""
    entry = FakeEntry(1, datetime.datetime(2026, 7, 1, tzinfo=UTC))
    entry.summary = '<p>Hello &amp; <b>welcome</b></p>'
    feed_box['feed'] = FakeFeed([entry])
    config = {'feeds': [{'name': 'Example', 'url': FEED_URL, 'source_mode': 'text'}]}

    candidates, _, _ = rb.check_rss_feeds(config, {})

    assert candidates[0]['source_mode'] == 'text'
    assert candidates[0]['summary'] == 'Hello & welcome'  # タグは落ちて実体参照は戻る


def test_default_source_mode_is_url(feed_box):
    candidates, _, _ = rb.check_rss_feeds(CONFIG, {})
    assert candidates[0]['source_mode'] == 'url'
    assert rb.source_add_args(candidates[0], 'nb1')[2] == candidates[0]['link']


def test_notification_marks_summary_only_articles(monkeypatch):
    """要約しか入っていない記事は、Slack上でもそうと分かるようにする。"""
    posted = []
    monkeypatch.setattr(rb, '_post_webhook', lambda url, payload, label: posted.append(payload))
    rb.send_notification('https://hooks.slack.com/x', [TEXT_ARTICLE, article('https://a.example/1', feed_name='F1')])

    text = posted[0]['text']
    assert 'OpenAI・要約のみ' in text
    assert '（F1）' in text  # 通常のURL投入分には印を付けない


def test_generate_audio_passes_prompt_and_length(monkeypatch):
    calls = []
    monkeypatch.setattr(rb.subprocess, 'run', lambda cmd, **kw: calls.append(cmd))
    rb.generate_audio('nb1', language='ja', prompt='対談形式で', length='long')
    cmd = calls[0]
    assert cmd[-1] == '対談形式で' and '--length' in cmd and 'long' in cmd


# --- 失敗の説明文（Slack へ出す中身） -----------------------------------------

AUTH_EXPIRED_STDOUT = (
    '{\n'
    '  "error": true,\n'
    '  "code": "UNEXPECTED_ERROR",\n'
    '  "message": "Unexpected error: Authentication expired or invalid. '
    'Redirected to: https://accounts.google.com/<redacted>\\nRun \'notebooklm login\' to re-authenticate."\n'
    '}\n'
)


def _cli_failure(stdout, returncode=2):
    return subprocess.CalledProcessError(returncode, ['notebooklm', 'list', '--json'], output=stdout, stderr='')


def test_auth_expiry_is_named_instead_of_a_bare_exit_code():
    """exit 2 だけでは認証切れとバグの区別がつかない。code と message を必ず添える。"""
    msg = rb.describe_failure(_cli_failure(AUTH_EXPIRED_STDOUT))

    assert 'Exit code: 2' in msg
    assert 'UNEXPECTED_ERROR' in msg
    assert 'Authentication expired or invalid' in msg
    # 復旧手順まで出す（次に落ちた人がそのまま実行できること）
    assert 'gh secret set NOTEBOOKLM_AUTH_JSON' in msg
    # CI 専用プロファイルを案内すること。default を押し戻すと分離が無意味になる
    assert 'notebooklm -p ci login' in msg
    assert 'profiles/ci/storage_state.json' in msg
    assert 'profiles/default' not in msg


def test_non_auth_cli_error_reports_code_without_the_auth_hint():
    stdout = '{"error": true, "code": "NOT_FOUND", "message": "Notebook abc not found"}'
    msg = rb.describe_failure(_cli_failure(stdout, returncode=1))

    assert 'NOT_FOUND: Notebook abc not found' in msg
    assert 'gh secret set' not in msg


def test_unparsable_cli_output_falls_back_to_exit_code():
    """封筒が読めないときに例外を出さず、従来どおりの情報で通知できること。"""
    for stdout in ('Just some plain text', '', None, '{"not": "an envelope"}', '[1, 2, 3]'):
        msg = rb.describe_failure(_cli_failure(stdout))
        assert msg.startswith('NotebookLM command failed.')
        assert 'Exit code: 2' in msg


def test_raw_cli_output_is_never_copied_into_the_message():
    """封筒の既知フィールドだけを拾う。未知フィールドはクッキーを運びうるので載せない。"""
    stdout = (
        '{"error": true, "code": "RPC_ERROR", "message": "boom", '
        '"response_body": "__Secure-1PSID=SUPERSECRETCOOKIEVALUE"}'
    )
    msg = rb.describe_failure(_cli_failure(stdout))

    assert 'RPC_ERROR: boom' in msg
    assert 'SUPERSECRETCOOKIEVALUE' not in msg
    assert 'response_body' not in msg


def test_non_subprocess_exception_keeps_its_type_name():
    assert rb.describe_failure(ValueError('boom')) == 'ValueError: boom'


def test_timeout_does_not_masquerade_as_a_cli_error():
    e = subprocess.TimeoutExpired(['notebooklm', 'list', '--json'], 300)
    msg = rb.describe_failure(e)

    assert msg.startswith('TimeoutExpired:')
    assert 'Exit code' not in msg
