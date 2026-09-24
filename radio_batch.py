import datetime
import html
import json
import os
import re
import subprocess
import sys
import time
from types import SimpleNamespace
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import feedparser
import httpx
import yaml

# パス設定
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.yaml')
STATE_PATH = os.path.join(BASE_DIR, 'state.json')

JST = ZoneInfo('Asia/Tokyo')
# ノートブックの日付や月次判定に使うタイムゾーン。settings.timezone で上書きできる。
# runner は UTC なので、ここを経由しないと朝の回のノートブックが前日名になる。
LOCAL_TZ = JST
UTC = datetime.UTC
EPOCH = datetime.datetime(1970, 1, 1, tzinfo=UTC)

# 1回の実行で1フィードから処理する記事数の上限（settings.limits.per_feed で上書き可）
MAX_ARTICLES_PER_FEED = 3
# 1回の実行で処理する記事数の全体上限（settings.limits.total で上書き可）。
# 記事ごとに source wait が直列で走るため、上げすぎると30分タイムアウトを圧迫する。
MAX_ARTICLES_TOTAL = 15
# 透かしと同時刻のエントリを識別するために保持するID数
MAX_RECENT_IDS = 200
# feed に topic が指定されていない場合の行き先ノートブック（settings.default_topic で上書き可）
DEFAULT_TOPIC = 'AI'

# feed の source_mode。既定は 'url'（記事URLを渡して NotebookLM に取得させる）。
# 'text' は記事URLを渡さず、RSSの配信内容をこちら側でMarkdown化して投入するモード。
# openai.com のように Cloudflare のボット判定で記事ページが 403 (cf-mitigated: challenge)
# を返すホスト向け。相手側の判定なのでリトライでは通らず、フェッチャーを迂回するしかない。
TEXT_SOURCE_MODE = 'text'

# ボット対策ページを掴まされたソースのタイトル (Cloudflare "Just a moment..." 等)。
# source add は成功するのに中身は検証ページ、という検知しづらい失敗を拾うための判定。
JUNK_TITLE_RE = re.compile(
    r'just a moment|attention required|access denied|verify you are human|enable javascript|are you a robot',
    re.IGNORECASE,
)

FEED_TIMEOUT = 30.0
NOTEBOOKLM_TIMEOUT = 300
SOURCE_WAIT_TIMEOUT = 600
# フィード取得時に名乗る User-Agent（settings.user_agent で上書き可）。
# 既定はこのソフトウェアのホームページを指す。fork 運用なら自分のリポジトリを名乗るとよい。
USER_AGENT = 'notebooklm-radio/1.0 (+https://github.com/inoueUJ/notebooklm-radio)'


def load_config():
    with open(CONFIG_PATH, encoding='utf-8') as f:
        return yaml.safe_load(f)


def apply_settings_overrides(config):
    """config の settings でモジュール既定値を上書きする。main() の冒頭で一度だけ呼ぶ。

    テストは各関数を直接呼ぶ（main を通らない）ので、既定値がそのまま使われる。
    上書き対象を増やしたら README の設定リファレンスも更新すること。
    """
    global LOCAL_TZ, DEFAULT_TOPIC, MAX_ARTICLES_PER_FEED, MAX_ARTICLES_TOTAL, USER_AGENT
    settings = (config or {}).get('settings') or {}
    if settings.get('timezone'):
        LOCAL_TZ = ZoneInfo(settings['timezone'])
    DEFAULT_TOPIC = settings.get('default_topic', DEFAULT_TOPIC)
    limits = settings.get('limits') or {}
    MAX_ARTICLES_PER_FEED = int(limits.get('per_feed', MAX_ARTICLES_PER_FEED))
    MAX_ARTICLES_TOTAL = int(limits.get('total', MAX_ARTICLES_TOTAL))
    USER_AGENT = settings.get('user_agent', USER_AGENT)


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Failed to load state.json ({e}). Starting with empty state.")
            return {}
    return {}


def save_state(state):
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)


def redact(text):
    """Secret がエラー通知経由で外部に漏れるのを防ぐ。"""
    text = str(text)
    for name in ('NOTEBOOKLM_AUTH_JSON', 'NOTIFY_WEBHOOK_URL', 'GITHUB_TOKEN'):
        value = os.environ.get(name)
        if value and len(value) > 8:
            text = text.replace(value, f'<{name} redacted>')

    # NOTEBOOKLM_AUTH_JSON は JSON なので、個々のクッキー値が単体で現れることがある
    raw_auth = os.environ.get('NOTEBOOKLM_AUTH_JSON')
    if raw_auth:
        try:
            for token in _iter_strings(json.loads(raw_auth)):
                if len(token) > 16:
                    text = text.replace(token, '<redacted>')
        except Exception:
            pass

    return text[:1500]


def _iter_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_strings(v)


def run_notebooklm_json(args, retries=1):
    """notebooklm CLI を叩いて JSON を返す。retries>1 は冪等な操作にのみ使うこと。"""
    cmd = ['notebooklm'] + args + ['--json']
    # notebooklm login 等で取得した credentials (NOTEBOOKLM_AUTH_JSON) は環境変数として引き継がれます
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=NOTEBOOKLM_TIMEOUT)
            return json.loads(result.stdout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            last_error = e
            if attempt < retries:
                wait = 2**attempt
                print(f"Attempt {attempt}/{retries} failed ({type(e).__name__}). Retrying in {wait}s...")
                time.sleep(wait)
    raise last_error


AUTH_EXPIRED_HINT = (
    '認証切れ。CI 専用プロファイルで `notebooklm -p ci login` をやり直し、'
    '`gh secret set NOTEBOOKLM_AUTH_JSON < ~/.notebooklm/profiles/ci/storage_state.json` '
    'でシークレットを更新すること。環境変数モードは書き戻し先が無く自動更新されないため、放置しても直らない。'
    ' default プロファイル（普段使い）を押し戻さないこと。ローカル利用のたびに Cookie が'
    'ローテーションされ、CI にコピーした固定値の寿命が縮む。'
)


AUDIO_FAILED_NOTE = (
    '記事はノートブックに入ったが、音声の生成を開始できなかった。記事は既読にしてある'
    '（再実行しても同じ URL を二重投入するだけなので）。NotebookLM アプリから手動で生成できる。'
    'code が rate_limited なら 1 日の生成上限（無料 3 本 / Plus 6 本 / Pro 20 本）に当たっている。'
)

NOTEBOOK_URL_BASE = 'https://notebooklm.google.com/notebook/'


def _cli_error_envelope(stdout):
    """notebooklm CLI の JSON エラー封筒から code と message だけを取り出す。

    封筒以外（プレーンテキスト、JSON でない、error フラグが無い）は None を返す。
    """
    if not stdout:
        return None
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not payload.get('error'):
        return None
    parts = [str(payload[key]) for key in ('code', 'message') if payload.get(key)]
    return ': '.join(parts) or None


def describe_failure(e):
    """例外を、通知に載せられる説明文へ整形する。

    notebooklm CLI は `--json` 付きで失敗すると stdout に
    {"error": true, "code": ..., "message": ...} を出す。exit code だけを載せると
    認証切れもライブラリのバグも同じ 2 に潰れて区別がつかない（2026-08-05 の
    セッション失効に 2 週間気付けなかったのはこれが理由）。そこで封筒が読めた場合だけ
    code と message を添える。

    stdout/stderr の生の中身はセッションクッキーを運びうるので決して載せない。
    封筒の既知フィールドだけを拾うことで、redact() の後段防御に頼らずに済ませる。
    """
    if not isinstance(e, subprocess.CalledProcessError):
        return f"{type(e).__name__}: {e}"

    msg = f"NotebookLM command failed.\nCommand: {' '.join(e.cmd)}\nExit code: {e.returncode}"
    detail = _cli_error_envelope(e.stdout)
    if detail:
        msg += f"\n{detail}"
        if 'Authentication expired or invalid' in detail:
            msg += f"\n\n{AUTH_EXPIRED_HINT}"
    return msg


def get_or_create_notebook(title):
    print(f"Listing notebooks to find '{title}'...")
    notebooks_data = run_notebooklm_json(['list'], retries=3)
    for nb in notebooks_data.get('notebooks', []):
        if nb.get('title') == title:
            print(f"Found existing notebook: '{title}' (ID: {nb['id']})")
            return nb['id']

    print(f"Notebook '{title}' not found. Creating new one...")
    res = run_notebooklm_json(['create', title])
    return res['notebook']['id']


def strip_html(text):
    """RSSの配信文からタグを落としてプレーンテキストにする。"""
    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', text, flags=re.S | re.I)
    text = re.sub(r'<br\s*/?>|</p\s*>|</div\s*>|</li\s*>', '\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return re.sub(r'\n{3,}', '\n\n', html.unescape(text)).strip()


def entry_summary(entry):
    """エントリの本文（配信されている範囲）をプレーンテキストで返す。

    content:encoded を配信しているフィードは全文が入ることもあるが、
    OpenAI のように description だけ（=要約のみ）のフィードもある。
    """
    contents = getattr(entry, 'content', None)
    raw = (contents[0].get('value') if contents else '') or getattr(entry, 'summary', '') or ''
    return strip_html(raw)


def build_text_source(article):
    """記事URLの代わりに投入するテキストソースをMarkdownで組み立てる。

    NotebookLM のフェッチャーを通らないのでボット判定に引っかからない。ただし中身は
    RSSが配信している範囲であって記事の全文とは限らない。ラジオが「全文を読んだ」前提で
    語ると誤情報になるので、その但し書きを本文の先頭に必ず入れる。
    """
    summary = article.get('summary') or ''
    ts = article.get('ts')
    published = ts.astimezone(LOCAL_TZ).strftime('%Y-%m-%d') if ts else '不明'

    if summary:
        note = (
            '配信元サイトのボット対策により記事本文を取得できないため、'
            '公式RSSが配信している要約文のみを収録しています。**記事の全文ではありません。**'
        )
        body = f"## 配信されている要約（RSS原文のまま）\n\n{summary}"
    else:
        note = (
            '配信元サイトのボット対策により記事本文を取得できず、RSSにも要約文がないため、'
            '**タイトルと公開日しか判明していません。**'
        )
        body = '## 本文\n\n（RSSに要約文が含まれていないため、タイトル以外の情報はありません）'

    return (
        f"# {article['title']}\n\n"
        f"> ⚠️ {note}\n"
        f"> ここに書かれていない内容を推測で補わず、詳細は「未確認」として扱ってください。\n\n"
        f"- 出典: {article['feed_name']}\n"
        f"- URL: {article['link']}\n"
        f"- 公開日: {published}\n\n"
        f"{body}\n"
    )


def source_add_args(article, notebook_id):
    """`notebooklm source add` の引数を組み立てる。"""
    if article.get('source_mode') != TEXT_SOURCE_MODE:
        return ['source', 'add', article['link'], '--notebook', notebook_id]
    # --title=... の形にするのは、記事タイトルが '-' で始まってもオプション扱いされないため
    return [
        'source',
        'add',
        build_text_source(article),
        '--type',
        'text',
        f"--title={article['title']}",
        '--notebook',
        notebook_id,
    ]


def add_sources_and_wait(notebook_id, articles):
    """ソースを追加して読み込みを待つ。(音声を生成してよいか, 取り込めなかった [(url, title)]) を返す。

    1件の失敗でバッチ全体を止めない。1件も追加できなかった場合のみ例外を投げる。
    add が成功しても、サイトのボット対策ページを掴まされていることがある
    (タイトルが "Just a moment..." 等)。中身がないので削除し、呼び出し元に報告する。
    """
    source_ids = {}  # url -> source_id
    fetched_ids = {}  # うち NotebookLM にURLを取得させたもの。ボット対策ページ判定の対象
    failed_urls = []
    for article in articles:
        url = article['link']
        is_text = article.get('source_mode') == TEXT_SOURCE_MODE
        try:
            print(f"Adding {'text ' if is_text else ''}source: {url}")
            res = run_notebooklm_json(source_add_args(article, notebook_id))
            source_ids[url] = res['source']['id']
            if not is_text:
                fetched_ids[url] = res['source']['id']
        except Exception as e:
            print(f"Warning: Failed to add source {url}: {redact(e)}")
            failed_urls.append(url)

    if not source_ids:
        raise RuntimeError(f"All sources failed to add: {failed_urls}")

    if failed_urls:
        print(f"Note: {len(failed_urls)} source(s) failed, continuing with {len(source_ids)} source(s).")

    # 読み込み待ちも個別に失敗を許容する（add と同じ方針）
    ready = 0
    for sid in source_ids.values():
        print(f"Waiting for source to be ready: {sid}")
        try:
            subprocess.run(
                ['notebooklm', 'source', 'wait', sid, '--notebook', notebook_id],
                check=True,
                timeout=SOURCE_WAIT_TIMEOUT,
            )
            ready += 1
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            print(f"Warning: Source {sid} did not become ready: {type(e).__name__}")

    if ready == 0:
        raise RuntimeError('No source became ready; skipping audio generation.')

    # 判定はURL投入分だけ。テキスト投入分はフェッチャーを通っていないので構造上
    # ボット対策ページになり得ず、こちらが付けたタイトルを誤検知するだけになる。
    blocked = remove_junk_sources(notebook_id, fetched_ids)
    usable = len(source_ids) - len(blocked)
    print(f"{ready}/{len(source_ids)} source(s) ready, {len(blocked)} junk removed, {usable} usable.")
    # 全部ボット対策ページだった場合は音声を生成しない（古いソースだけで空回りさせない）
    return usable > 0, blocked


def remove_junk_sources(notebook_id, source_ids):
    """ボット対策ページを掴んだソースを検出して削除し、[(url, title)] を返す。

    検出に失敗しても本体は止めない。見逃しはラジオの質の問題であって配信は壊れないので、
    警告ログにとどめて続行する。
    """
    if not source_ids:
        return []
    try:
        listing = run_notebooklm_json(['source', 'list', '--notebook', notebook_id], retries=2)
    except Exception as e:
        print(f"Warning: source list failed; skipping junk detection: {redact(e)}")
        return []

    titles = {s.get('id'): s.get('title') or '' for s in listing.get('sources', [])}
    blocked = []
    for url, sid in source_ids.items():
        title = titles.get(sid, '')
        if not JUNK_TITLE_RE.search(title):
            continue
        print(f"Junk source detected for {url} (title: '{title}'); removing it.")
        try:
            # -y は必須。source delete は確認プロンプトを出すので、TTY のない CI では
            # EOF で Abort になり削除が成立しない（junk を含んだまま音声が生成される）
            run_notebooklm_json(['source', 'delete', sid, '-y', '--notebook', notebook_id])
        except Exception as e:
            print(f"Warning: failed to remove junk source {sid}: {redact(e)}")
        blocked.append((url, title))
    return blocked


def generate_audio(notebook_id, language='ja', prompt=None, length=None):
    print(f"Triggering audio generation (language: {language})...")
    # --no-wait のため、これは「生成の開始」であって「完了」ではない。
    # --json 付き(run_notebooklm_json)で呼ぶのは、失敗時に CLI の封筒(code/message)を通知に
    # 載せるため。日次の生成上限に当たると rate_limited で失敗するが、exit code だけでは分からない。
    args = ['generate', 'audio', '--no-wait', '--notebook', notebook_id, '--language', language]
    if length:
        args += ['--length', length]
    if prompt:
        args.append(prompt)
    result = run_notebooklm_json(args)
    # 標準出力を捕捉したので、ログには要点だけ残す
    print(f"Audio generation {result.get('status', 'triggered')} (task: {result.get('task_id')})")
    return result


def _post_webhook(webhook_url, payload, label):
    try:
        response = httpx.post(webhook_url, json=payload, timeout=15.0)
        response.raise_for_status()
        print(f"{label} sent successfully.")
    except Exception as e:
        print(f"Failed to send {label.lower()}: {redact(e)}")


def send_notification(webhook_url, new_articles, blocked=None, notebooks=None, no_audio=None):
    """新着一覧を通知する。

    notebooks: {topic: (title, notebook_id)}。見出しにノートブック名とリンクを載せる
    （無いと、アプリで命名規則からノートブックを探すことになる）。
    no_audio: 音声の生成を開始できなかった/しなかったトピックの集合。「開始した」と嘘をつかない。
    """
    if not webhook_url:
        print("No notification Webhook URL configured. (NOTIFY_WEBHOOK_URL is empty)")
        return

    is_discord = "discord.com" in webhook_url
    notebooks = notebooks or {}
    no_audio = no_audio or set()

    # 参照元URLも載せる（後から出典を辿れるように）
    def fmt(art):
        # テキスト投入分はRSSの要約しか入っていない。ラジオの情報量が違うので明示する
        note = '・要約のみ' if art.get('source_mode') == TEXT_SOURCE_MODE else ''
        if is_discord:
            return f"・{art['title']}（{art['feed_name']}{note}）\n  {art['link']}"
        return f"・<{art['link']}|{art['title']}>（{art['feed_name']}{note}）"

    # トピック(=ノートブック)ごとに見出しを付ける。topic なしの記事は見出しなしでそのまま並べる
    by_topic = {}
    for art in new_articles:
        by_topic.setdefault(art.get('topic'), []).append(art)
    sections = []
    for topic, arts in by_topic.items():
        lines = "\n".join(fmt(a) for a in arts)
        header = f"《{topic}》" if topic else ''
        if topic in notebooks:
            title, notebook_id = notebooks[topic]
            url = f"{NOTEBOOK_URL_BASE}{notebook_id}"
            header += f" {title}\n  {url}" if is_discord else f" <{url}|{title}>"
        if topic in no_audio:
            header += "（音声は未生成）"
        sections.append(f"{header}\n{lines}" if header else lines)

    parts = []
    if new_articles:
        # どのフィード（企業）から新着があったかを集計
        feed_names = list(dict.fromkeys([art['feed_name'] for art in new_articles]))  # 順序を保ったユニーク化
        sources = "、".join(feed_names)
        started = [t for t in by_topic if t not in no_audio]
        status = (
            "ラジオの生成を開始したよ〜"
            if started
            else "ラジオの生成は開始できなかったよ（理由は下の注記か別の通知を見てね）"
        )
        parts.append(f"🎙️ {sources} が新しい記事出してたよ（{len(new_articles)}件）\n{status}")
        parts.extend(sections)
    if blocked:
        blocked_lines = "\n".join(f"・{url}" for url, _title in blocked)
        parts.append(f"⚠️ 以下はサイトのボット対策で本文を取り込めなかったから、ラジオには入ってないよ\n{blocked_lines}")
    if not parts:
        return

    text = "\n".join(parts)
    payload = {"content": text} if is_discord else {"text": text}
    _post_webhook(webhook_url, payload, 'Notification')


def send_no_news_notification(webhook_url):
    """新着ゼロでも一報入れる。沈黙が「新着なし」なのか「故障」なのか区別できるように。"""
    if not webhook_url:
        return
    is_discord = "discord.com" in webhook_url
    text = "😪 今回は新着なしだったよ。ラジオはおやすみ〜"
    _post_webhook(webhook_url, {"content": text} if is_discord else {"text": text}, 'No-news notification')


def send_maintenance_warning(webhook_url, text):
    """メンテナンス（cleanup 等）の失敗。フィード取得の問題とは別の見出しで出す。"""
    if not webhook_url:
        return
    is_discord = "discord.com" in webhook_url
    _post_webhook(webhook_url, {"content": text} if is_discord else {"text": text}, 'Maintenance warning')


def send_feed_warning(webhook_url, warnings):
    if not webhook_url or not warnings:
        return

    is_discord = "discord.com" in webhook_url
    lines = "\n".join(f"・{name}: {reason}" for name, reason in warnings)
    text = f"⚠️ RSSフィードの取得に問題があるよ\n{lines}"
    payload = {"content": text} if is_discord else {"text": text}
    _post_webhook(webhook_url, payload, 'Feed warning')


def send_error_notification(webhook_url, error_message, notebook_title=None):
    if not webhook_url:
        print("No notification Webhook URL configured. (NOTIFY_WEBHOOK_URL is empty)")
        return

    is_discord = "discord.com" in webhook_url
    error_message = redact(error_message)

    # GitHub Actionsの実行URLを組み立てる（環境変数から取得）
    github_run_url = ""
    server_url = os.environ.get('GITHUB_SERVER_URL')
    repository = os.environ.get('GITHUB_REPOSITORY')
    run_id = os.environ.get('GITHUB_RUN_ID')
    if server_url and repository and run_id:
        github_run_url = f"\n*実行ログ:* <{server_url}/{repository}/actions/runs/{run_id}|GitHub Actions Run>"

    if is_discord:
        notebook_info = f"ノートブック「{notebook_title}」" if notebook_title else "バッチ処理"
        discord_run_url = f"\n**実行ログ:** <{server_url}/{repository}/actions/runs/{run_id}>" if github_run_url else ""
        text = (
            f"⚠️ **Tech Radio 実行エラー発生**\n"
            f"{notebook_info}の処理中にエラーが発生しました。\n\n"
            f"**エラー内容:**\n```{error_message}```"
            f"{discord_run_url}"
        )
        payload = {"content": text}
    else:
        # Slack Block Kit フォーマット
        notebook_info = f"ノートブック「*{notebook_title}*」" if notebook_title else "バッチ処理"
        payload = {
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": "⚠️ Tech Radio 実行エラー発生", "emoji": True},
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"{notebook_info} の処理中にエラーが発生しました。"},
                },
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*エラー内容:*\n```{error_message}```"}},
            ]
        }
        if github_run_url:
            payload["blocks"].append({"type": "context", "elements": [{"type": "mrkdwn", "text": github_run_url}]})

    _post_webhook(webhook_url, payload, 'Error notification')


# ---------------------------------------------------------------------------
# メンテナンス: 古いラジオの削除 / 停滞フィードの検知
#
# 削除は「このバッチが作ったノートブックだけ」が対象。タイトルが
# notebook_title_format の日付形式に完全一致しない限り候補にすら入らないので、
# 手動で作ったノートブックには構造的に触れない。
# ---------------------------------------------------------------------------


def _notebook_title_patterns(config):
    """削除対象を判定する正規表現の一覧。完全一致のみ（手動ノートを守る唯一の砦）。

    {topic} は config に実在するトピック名だけの選択肢に展開する。任意文字にマッチ
    させると「バッチ命名かどうか」の保証が消えるので、絶対に緩めないこと。
    """
    settings = config.get('settings', {})
    templates = [settings.get('notebook_title_format', 'Tech Radio {date}')]
    templates += (settings.get('cleanup') or {}).get('legacy_title_formats') or []
    topics = {f.get('topic', DEFAULT_TOPIC) for f in config.get('feeds', [])} | {DEFAULT_TOPIC}
    topic_re = '(?:' + '|'.join(re.escape(t) for t in sorted(topics)) + ')'

    patterns = []
    for template in templates:
        escaped = re.escape(template)
        escaped = escaped.replace(re.escape('{date}'), r'(\d{4}-\d{2}-\d{2})')
        escaped = escaped.replace(re.escape('{topic}'), topic_re)
        patterns.append(re.compile(escaped))
    return patterns


def cleanup_old_notebooks(config, webhook_url, now=None):
    settings = config.get('settings', {})
    cleanup_cfg = settings.get('cleanup') or {}
    if not cleanup_cfg.get('enabled', False):
        return

    retention_days = int(cleanup_cfg.get('retention_days', 7))
    dry_run = bool(cleanup_cfg.get('dry_run', True))
    # "Tech Radio {topic} {date}" → ^Tech\ Radio\ (?:AI|Infra)\ (\d{4}-\d{2}-\d{2})$ 等、完全一致で判定
    patterns = _notebook_title_patterns(config)

    now = now or datetime.datetime.now(LOCAL_TZ)
    cutoff = (now.astimezone(LOCAL_TZ) - datetime.timedelta(days=retention_days)).date()

    notebooks = run_notebooklm_json(['list'], retries=3).get('notebooks', [])
    targets = []
    for nb in notebooks:
        title = nb.get('title') or ''
        m = None
        for pattern in patterns:
            m = pattern.fullmatch(title)
            if m:
                break
        if not m:
            continue  # バッチ命名でないものは絶対に触らない
        try:
            nb_date = datetime.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if nb_date < cutoff:
            targets.append(nb)

    if not targets:
        print(f"Cleanup: no notebooks older than {retention_days} days.")
        return

    deleted, failed = [], []
    for nb in targets:
        if dry_run:
            print(f"Cleanup (dry-run): would delete '{nb['title']}' (ID: {nb['id']})")
            deleted.append(nb['title'])
            continue
        try:
            run_notebooklm_json(['delete', '-n', nb['id'], '-y'])
            print(f"Cleanup: deleted '{nb['title']}' (ID: {nb['id']})")
            deleted.append(nb['title'])
        except Exception as e:
            print(f"Cleanup: failed to delete '{nb['title']}': {redact(e)}")
            failed.append(nb['title'])

    if webhook_url:
        lines = '\n'.join(f"・{t}" for t in deleted)
        if dry_run:
            text = f"🧹 [ドライラン] {retention_days}日より古いラジオ {len(deleted)}件が削除対象だよ（まだ消してない）\n{lines}"
        else:
            text = f"🧹 {retention_days}日より古いラジオ {len(deleted)}件を削除したよ\n{lines}"
        if failed:
            text += '\n⚠️ 削除に失敗:\n' + '\n'.join(f"・{t}" for t in failed)
        is_discord = 'discord.com' in webhook_url
        _post_webhook(webhook_url, {'content': text} if is_discord else {'text': text}, 'Cleanup notification')


def check_stale_feeds(config, state, webhook_url, now=None):
    """毎月1日(settings.timezone 基準)に、30日以上新着のないフィードを知らせる。"""
    now = now or datetime.datetime.now(LOCAL_TZ)
    if now.astimezone(LOCAL_TZ).day != 1:
        return

    stale_days = int(config.get('settings', {}).get('stale_feed_days', 30))
    stale = []
    for feed_cfg in config.get('feeds', []):
        raw = state.get(feed_state_key(feed_cfg))
        last_new = raw.get('last_new') if isinstance(raw, dict) else None
        if not last_new:
            continue
        age = (now.astimezone(UTC) - datetime.datetime.fromisoformat(last_new)).days
        if age >= stale_days:
            stale.append((feed_cfg.get('name'), age))

    if not stale or not webhook_url:
        return
    lines = '\n'.join(f"・{name}: {age}日間新着なし" for name, age in stale)
    text = f"🩺 月次ヘルスチェック: 長期間新着のないフィードがあるよ（配信停止やサイト構造の変化かも）\n{lines}"
    is_discord = 'discord.com' in webhook_url
    _post_webhook(webhook_url, {'content': text} if is_discord else {'text': text}, 'Stale feed notification')


def run_maintenance(config, state, webhook_url):
    """本体処理の成否に影響させないメンテナンス。失敗しても警告通知のみ。"""
    try:
        cleanup_old_notebooks(config, webhook_url)
    except Exception as e:
        print(f"Warning: cleanup failed: {redact(e)}", file=sys.stderr)
        send_maintenance_warning(webhook_url, f'🧹 古いラジオの削除に失敗したよ（{type(e).__name__}）。次回また試すよ')
    try:
        check_stale_feeds(config, state, webhook_url)
    except Exception as e:
        print(f"Warning: stale feed check failed: {redact(e)}", file=sys.stderr)


# ---------------------------------------------------------------------------
# ページ更新の監視 (watch)
#
# サポートページなど「聴くコンテンツではないが即時に知りたい」更新を Slack に
# 通知するだけの経路。ラジオ（NotebookLM/音声）には一切流さない。
#
# 既読判定は watermark ではなく URL→lastmod の辞書で行う。RSS と違い同じ記事の
# lastmod が何度も動く（=再浮上する）ので、単一時刻の透かしでは表現できない。
# キーワードで絞った母集合は有界なので、辞書を丸ごと持っても肥大しない。
# ---------------------------------------------------------------------------


def send_watch_notification(webhook_url, name, updates):
    if not webhook_url:
        print("No notification Webhook URL configured. (NOTIFY_WEBHOOK_URL is empty)")
        return
    is_discord = 'discord.com' in webhook_url
    if is_discord:
        lines = '\n'.join(f"・[{kind}] {art['title']}\n  {art['link']}" for art, kind in updates)
    else:
        lines = '\n'.join(f"・[{kind}] <{art['link']}|{art['title']}>" for art, kind in updates)
    text = f"📌 {name} のページが更新されたよ（{len(updates)}件）\n{lines}"
    _post_webhook(webhook_url, {'content': text} if is_discord else {'text': text}, 'Watch notification')


def check_watch_pages(config, state, webhook_url):
    """config の watch 対象を確認し、更新を通知して state を進める。warnings を返す。"""
    warnings = []
    for watch_cfg in config.get('watch', []):
        name = watch_cfg.get('name')
        url = watch_cfg.get('url')
        prefix = watch_cfg.get('prefix')
        keywords = [k.lower() for k in watch_cfg.get('keywords', [])]
        print(f"Checking watch target: {name} ({url})")

        feed, problem = fetch_sitemap_feed(url, prefix)
        if problem:
            # 取得失敗時は state に触らない（一時的な404で全記事が「新規」に戻る事故を防ぐ）
            print(f"[watch:{name}] Warning: {problem}")
            warnings.append((name, problem))
            continue

        state_key = f'watch:{url}:{prefix}'
        raw = state.get(state_key)
        seen = raw.get('lastmod') if isinstance(raw, dict) else None
        first_run = seen is None

        current = {}
        updates = []
        for entry in feed.entries:
            slug = entry.link.lower()
            if keywords and not any(k in slug for k in keywords):
                continue
            ts = entry_time(entry)
            lastmod = ts.isoformat() if ts else ''
            current[entry.link] = lastmod
            if first_run:
                continue
            title = re.sub(r'^\d+\s*', '', entry.title)  # スラッグ先頭の記事ID番号を除く
            article = {'title': title, 'link': entry.link}
            if entry.link not in seen:
                updates.append((article, '新規'))
            elif seen[entry.link] != lastmod:
                updates.append((article, '更新'))

        if first_run:
            print(f"[watch:{name}] First run: recording {len(current)} matched pages without notifying.")
        elif updates:
            for art, kind in updates:
                print(f"[watch:{name}] {kind}: {art['link']}")
            send_watch_notification(webhook_url, name, updates)
        else:
            print(f"[watch:{name}] No changes among {len(current)} matched pages.")

        # sitemap から消えたページは追跡をやめる（current で丸ごと置き換え）
        state[state_key] = {'lastmod': current}

    return warnings


# ---------------------------------------------------------------------------
# フィードの取得と既読判定
#
# 既読判定は「透かし(watermark)」= 前回処理した最新記事の公開時刻 で行う。
# 「既読IDの一覧」で判定すると、リストを有界にした瞬間に溢れた記事が未読へ戻る。
# 時刻は単調増加するので、フィードが何件返そうと、IDを何件捨てようと壊れない。
# ---------------------------------------------------------------------------


def entry_time(entry):
    """エントリの公開時刻を aware な UTC datetime で返す。取得できなければ None。"""
    for attr in ('published_parsed', 'updated_parsed'):
        parsed = getattr(entry, attr, None)
        if parsed:
            return datetime.datetime(*parsed[:6], tzinfo=UTC)
    return None


def entry_key(entry):
    return getattr(entry, 'id', None) or getattr(entry, 'link', None)


def fetch_feed(url):
    """フィードを取得する。(feed, 問題の説明 or None) を返す。"""
    try:
        response = httpx.get(url, timeout=FEED_TIMEOUT, follow_redirects=True, headers={'User-Agent': USER_AGENT})
    except Exception as e:
        return None, f"取得失敗: {type(e).__name__}"

    if response.status_code >= 400:
        return None, f"HTTP {response.status_code}"

    feed = feedparser.parse(response.content)
    if not feed.entries:
        reason = "エントリが0件"
        if getattr(feed, 'bozo', False):
            reason += f" (パース失敗: {type(feed.bozo_exception).__name__})"
        return None, reason

    return feed, None


def fetch_sitemap_feed(url, prefix):
    """RSSを配信しないサイト向け: sitemap.xml をフィードに見立てる。

    prefix 配下のURLだけを記事として扱い、<lastmod> があれば公開時刻代わりに使う。
    lastmod は「最終更新日」なので、既存記事の修正で再浮上しうる。
    lastmod の無い sitemap (claude.com など) では ID のみで新着判定される。
    """
    try:
        response = httpx.get(url, timeout=FEED_TIMEOUT, follow_redirects=True, headers={'User-Agent': USER_AGENT})
    except Exception as e:
        return None, f"取得失敗: {type(e).__name__}"

    if response.status_code >= 400:
        return None, f"HTTP {response.status_code}"

    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError as e:
        return None, f"パース失敗: {type(e).__name__}"

    ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    entries = []
    for node in root.findall('sm:url', ns):
        loc = node.findtext('sm:loc', default='', namespaces=ns).strip()
        if not loc.startswith(prefix) or loc.rstrip('/') == prefix.rstrip('/'):
            continue
        entry = SimpleNamespace(
            id=loc,
            link=loc,
            title=loc.rstrip('/').rsplit('/', 1)[-1].replace('-', ' '),
        )
        lastmod = node.findtext('sm:lastmod', default='', namespaces=ns).strip()
        if lastmod:
            try:
                parsed = datetime.datetime.fromisoformat(lastmod.replace('Z', '+00:00'))
                entry.published_parsed = parsed.utctimetuple()
            except ValueError:
                pass
        entries.append(entry)

    if not entries:
        return None, f"prefix に一致するURLが0件: {prefix}"
    return SimpleNamespace(entries=entries), None


def feed_state_key(feed_cfg):
    """state のキー。

    sitemap 型は同一URLを prefix 違いで複数フィードが共有しうる（例: anthropic.com の
    /news/ と /engineering/）。URLだけをキーにすると2フィードが1つの既読状態を奪い合い、
    片方の記事が黙ってスキップされるので、キーに prefix を含めて分離する。
    """
    if feed_cfg.get('type') == 'sitemap':
        return f"sitemap:{feed_cfg['url']}:{feed_cfg['prefix']}"
    return feed_cfg['url']


def read_sitemap_state(state, key, legacy_url, entries):
    """sitemap 型フィードの既読URL集合を返す。(seen or None, 移行したか)。None は真の初回。

    旧形式（URLキー + watermark）が残っていれば、そこから既読集合を復元する。
    旧形式で「既読」だった条件 = recent_ids に載っている、または lastmod が透かし以前。
    """
    raw = state.get(key)
    if isinstance(raw, dict) and 'seen' in raw:
        return set(raw['seen']), False

    legacy = state.get(legacy_url)
    if isinstance(legacy, dict) and legacy.get('watermark'):
        watermark = datetime.datetime.fromisoformat(legacy['watermark'])
        recent = set(legacy.get('recent_ids', []))
        seen = {e['id'] for e in entries if e['id'] in recent or (e['ts'] and e['ts'] <= watermark)}
        print(f"Migrating legacy watermark state to a seen-set ({len(seen)} read) for {key}.")
        return seen, True

    return None, False


def read_feed_state(state, url):
    """(watermark, recent_ids) を返す。未初期化・旧形式(IDのリスト)は初回実行として扱う。"""
    raw = state.get(url)
    if isinstance(raw, dict):
        watermark = raw.get('watermark')
        parsed = datetime.datetime.fromisoformat(watermark) if watermark else None
        return parsed, list(raw.get('recent_ids', []))
    if isinstance(raw, list):
        print(f"Migrating legacy state for {url} (treating as first run).")
    return None, []


def is_unread(entry_id, ts, watermark, recent_ids):
    if entry_id in recent_ids:
        return False
    if watermark is None or ts is None:
        return True
    # 透かしと同時刻の記事は時刻では区別できない。処理済みなら recent_ids に載っているので、
    # 載っていなければ未読。日付だけのフィード(Changelog 系)では同じ日の記事が全部同時刻に
    # なるため、ここを `>` にすると上限で持ち越したはずの記事や、後から同日に追加された記事が
    # 黙って消える(2026-09-22 Cloudflare Changelog: 同日 7 件のうち 4 件を喪失)。
    return ts >= watermark


def check_rss_feeds(config, state):
    """(candidates, feed_results, warnings) を返す。state はまだ変更しない。"""
    candidates = []
    feed_results = []
    warnings = []

    for feed_cfg in config.get('feeds', []):
        name = feed_cfg.get('name')
        url = feed_cfg.get('url')
        topic = feed_cfg.get('topic', DEFAULT_TOPIC)
        source_mode = feed_cfg.get('source_mode', 'url')
        is_sitemap = feed_cfg.get('type') == 'sitemap'
        print(f"Checking feed: {name} ({url})")

        if is_sitemap:
            feed, problem = fetch_sitemap_feed(url, feed_cfg['prefix'])
        else:
            feed, problem = fetch_feed(url)
        if problem:
            print(f"[{name}] Warning: {problem}")
            warnings.append((name, problem))
            continue

        entries = []
        for entry in feed.entries:
            eid = entry_key(entry)
            link = getattr(entry, 'link', None)
            if not eid or not link:
                continue
            entries.append(
                {
                    'id': eid,
                    'ts': entry_time(entry),
                    'title': getattr(entry, 'title', '(no title)'),
                    'link': link,
                    'feed_name': name,
                    'feed_url': url,
                    'topic': topic,
                    # ボット対策で本文が取れないホストは、URLではなくRSSの配信内容を投入する
                    'source_mode': source_mode,
                    'summary': entry_summary(entry),
                }
            )

        if not entries:
            print(f"[{name}] Warning: 有効なエントリがありません")
            warnings.append((name, '有効なエントリがありません'))
            continue

        key = feed_state_key(feed_cfg)
        if is_sitemap:
            # sitemap は毎回全URLを返すので、既読URL集合の membership 判定が正確に成立する。
            # lastmod は「最終更新日」であり、古い記事の修正で動く。watermark(時刻)で判定すると
            # 4月の記事が7月の修正で「新着」に化けるので、lastmod は新着判定に使わない。
            seen, _migrated = read_sitemap_state(state, key, url, entries)
            first_run = seen is None
            unread = entries if first_run else [e for e in entries if e['id'] not in seen]
            result = {
                'name': name,
                'url': url,
                'key': key,
                'mode': 'sitemap',
                'entries': entries,
                'first_run': first_run,
                'seen': seen or set(),
            }
        else:
            watermark, recent_ids = read_feed_state(state, key)
            first_run = watermark is None
            recent_set = set(recent_ids)
            unread = [e for e in entries if is_unread(e['id'], e['ts'], watermark, recent_set)]
            result = {
                'name': name,
                'url': url,
                'key': key,
                'mode': 'rss',
                'entries': entries,
                'first_run': first_run,
                'watermark': watermark,
                'recent_ids': recent_ids,
            }

        unread.sort(key=lambda e: e['ts'] or EPOCH)

        if first_run:
            # 初回は最新1件だけ処理し、残りは既読にする（過去記事の洪水を防ぐ）
            picked = unread[-1:]
            print(f"[{name}] First run: marking all {len(entries)} entries as read, processing the latest one.")
        else:
            # 古い順に処理する。上限を超えた分は「既読にせず」次回に持ち越す
            picked = unread[:MAX_ARTICLES_PER_FEED]
            if len(unread) > len(picked):
                print(f"[{name}] {len(unread)} unread; processing {len(picked)}, carrying over the rest.")

        for article in picked:
            article['_first_run'] = first_run  # 全体上限(select_articles)で落とさないための印
            print(f"[{name}] New article: {article['title']}")

        candidates.extend(picked)
        feed_results.append(result)

    return candidates, feed_results, warnings


def _advance_sitemap_state(state, result, processed_ids, now):
    """sitemap 型の既読URL集合を進める。処理できなかった未読は集合に入れず持ち越す。"""
    current_ids = {e['id'] for e in result['entries']}
    picked_here = current_ids & processed_ids

    if result['first_run']:
        seen = current_ids  # 初回は全既読（最新1件だけ処理済み）
    else:
        # sitemap から消えたURLは追跡をやめる（集合が sitemap のサイズを超えて肥大しない）
        seen = (result['seen'] | picked_here) & current_ids

    key = result['key']
    prev = state.get(key)
    legacy = state.get(result['url'])
    entry = {'seen': sorted(seen)}
    if result['first_run'] or picked_here:
        entry['last_new'] = now.isoformat()
    else:
        # 新着なしでも last_new は引き継ぐ（停滞フィード検知のため）。移行時は旧形式から
        for source in (prev, legacy):
            if isinstance(source, dict) and source.get('last_new'):
                entry['last_new'] = source['last_new']
                break
    state[key] = entry
    # 旧形式（URLキー + watermark）は移行済みなので掃除する
    state.pop(result['url'], None)


def advance_state(state, feed_results, processed, now=None):
    """実際に処理できた記事の分だけ既読を進める。"""
    now = now or datetime.datetime.now(UTC)
    processed_ids = {a['id'] for a in processed}
    # 同じ URL を複数のフィードが配信していた場合、ノートブックに入ったのは 1 件でも
    # 全フィードで「処理済み」として既読を進める(dedupe_by_link と対)。GUID はフィードごとに違う
    processed_links = {a['link'] for a in processed if a.get('link')}

    def is_processed(entry):
        return entry['id'] in processed_ids or entry.get('link') in processed_links

    for result in feed_results:
        if result.get('mode') == 'sitemap':
            _advance_sitemap_state(state, result, processed_ids | processed_links, now)
            continue

        key = result['key']
        entries = result['entries']
        prev_watermark = result['watermark']

        if result['first_run']:
            # 初回のみ、フィード全体を既読にする
            dated = [e['ts'] for e in entries if e['ts']]
            new_watermark = max(dated) if dated else EPOCH
            # 公開時刻を持たないエントリも、初回はすべて既読にする
            dateless = [e['id'] for e in entries if e['ts'] is None]
        else:
            picked = [e for e in entries if is_processed(e)]
            if not picked:
                continue  # このフィードからは何も処理していない → 変更なし
            # 日付なしの記事しか処理しなかった場合も、既読(recent_ids)の記録は必要。
            # picked_ts が空なら透かしは進めず維持する。
            picked_ts = [e['ts'] for e in picked if e['ts']]
            new_watermark = max([prev_watermark, *picked_ts]) if picked_ts else prev_watermark
            # 2回目以降は、処理できたものだけを既読にする（残りは次回に持ち越す）
            dateless = [e['id'] for e in picked if e['ts'] is None]

        # 透かしと同時刻のエントリは時刻で区別できないので、IDで覚えておく。
        # 記録するのは「処理した」ものだけ(初回は全既読なので全件)。同時刻の未処理分まで載せると、
        # 持ち越したはずの記事が既読扱いで消える。is_unread が同時刻を未読に倒しているのは、
        # このリストが「処理済みの同時刻 ID」を正確に持つことが前提。
        tie_source = entries if result['first_run'] else picked
        ties = [e['id'] for e in tie_source if e['ts'] == new_watermark]

        merged = list(dict.fromkeys([*result['recent_ids'], *ties, *dateless]))
        kept = merged[-MAX_RECENT_IDS:]

        # 公開時刻を持たないIDと、透かしと同時刻のIDは、透かしでは既読判定できずこのリストにしか
        # 記録がない。上限で溢れさせると未読に戻ってしまうので、必ず残す。
        must_keep = set(dateless) | {e['id'] for e in entries if e['ts'] == new_watermark}
        rescued = [i for i in merged if i in must_keep and i not in kept]

        state[key] = {
            'watermark': new_watermark.isoformat(),
            'recent_ids': rescued + kept,
            # 最後に新着を確認した日時。停滞フィードの検知(check_stale_feeds)に使う。
            'last_new': now.isoformat(),
        }


def notebook_title_for(config, topic=None, now=None):
    """ノートブック名は LOCAL_TZ（既定 JST）基準。runner は UTC なので明示しないと朝の回が前日名になる。"""
    now = now or datetime.datetime.now(LOCAL_TZ)
    today_str = now.astimezone(LOCAL_TZ).strftime('%Y-%m-%d')
    template = config.get('settings', {}).get('notebook_title_format', 'Tech Radio {date}')
    return template.format(date=today_str, topic=topic or DEFAULT_TOPIC)


def dedupe_by_link(candidates):
    """フィードをまたいで同じ URL の記事を 1 つにする(先勝ち)。

    同じ記事を配信する 2 つのフィード(例: vercel.com/blog/feed と vercel.com/atom は同一内容)を
    両方購読すると、同じ URL が 2 回ソース投入され、通知に 2 行出て、1 回あたりの上限枠も
    2 つ消費していた。落とした側のフィードの既読は advance_state が URL で照合して進める。
    """
    unique, seen_links = [], set()
    for article in candidates:
        link = article.get('link')
        if link and link in seen_links:
            continue
        if link:
            seen_links.add(link)
        unique.append(article)
    if len(unique) < len(candidates):
        print(f"Dropped {len(candidates) - len(unique)} duplicate URL(s) shared across feeds.")
    return unique


def select_articles(candidates):
    """全体上限を適用する。初回実行分は各フィード1件なので常に通す。"""
    candidates = dedupe_by_link(candidates)
    first_run_articles = [a for a in candidates if a.get('_first_run')]
    rest = sorted([a for a in candidates if not a.get('_first_run')], key=lambda e: e['ts'] or EPOCH)

    remaining = max(MAX_ARTICLES_TOTAL - len(first_run_articles), 0)
    selected = first_run_articles + rest[:remaining]
    if len(selected) < len(candidates):
        print(f"Capping articles from {len(candidates)} to {len(selected)}.")
    return selected


def main():
    config = load_config()
    apply_settings_overrides(config)
    state = load_state()
    webhook_url = os.environ.get('NOTIFY_WEBHOOK_URL')
    notebook_title = None

    try:
        candidates, feed_results, warnings = check_rss_feeds(config, state)

        # ページ更新監視（Slack通知のみ、ラジオ化しない）。失敗しても本体は止めない
        try:
            warnings += check_watch_pages(config, state, webhook_url)
        except Exception as e:
            print(f"Warning: watch check failed: {redact(e)}", file=sys.stderr)
            warnings.append(('ページ更新監視', f'失敗: {type(e).__name__}'))

        if warnings:
            send_feed_warning(webhook_url, warnings)

        if not candidates:
            print("No new articles detected.")
            send_no_news_notification(webhook_url)
            advance_state(state, feed_results, [])
            save_state(state)
            run_maintenance(config, state, webhook_url)
            return

        new_articles = select_articles(candidates)
        print(f"Detected {len(new_articles)} new articles in total.")

        # トピック(AI / Infra)ごとに別ノートブックへ。混ぜるとラジオの話題が散らかるため
        by_topic = {}
        for art in new_articles:
            by_topic.setdefault(art.get('topic', DEFAULT_TOPIC), []).append(art)

        settings = config.get('settings', {})
        audio_cfg = settings.get('audio') or {}

        processed = []  # 実際にノートブックへ入った（または取り込み不能と確定した）記事
        blocked = []  # ボット対策ページで中身が取れなかった [(url, title)]
        failures = []  # (notebook_title, exception) ノートブック作成・ソース投入の失敗。記事は持ち越す
        audio_failures = []  # (notebook_title, exception) ソースは入ったが音声を開始できなかった。記事は既読
        notebooks = {}  # topic -> (title, notebook_id)。通知にノートブックへのリンクを載せる
        no_audio = set()  # 音声を開始しなかった/できなかったトピック。通知で「開始した」と言わない
        for topic, articles in by_topic.items():
            notebook_title = notebook_title_for(config, topic=topic)
            try:
                # 1. ノートブックの取得・作成
                notebook_id = get_or_create_notebook(notebook_title)
                notebooks[topic] = (notebook_title, notebook_id)

                # 2. ソースの追加 & 待機（ボット対策ページを掴んだソースはここで除去される）
                audio_ok, topic_blocked = add_sources_and_wait(notebook_id, articles)

                # 3. ラジオの生成トリガー
                if audio_ok:
                    try:
                        generate_audio(
                            notebook_id,
                            language=settings.get('language', 'ja'),
                            prompt=audio_cfg.get('prompt'),
                            length=audio_cfg.get('length'),
                        )
                    except Exception as e:
                        # ソースは既にノートブックに入っている。記事を未読のまま残すと次回また同じ URL を
                        # 投入して重複するだけなので既読にし、音声だけ失敗として別枠で報告する
                        # （日次の生成上限に当たると CLI は rate_limited で失敗する）
                        print(f"[{topic}] Audio generation failed: {redact(e)}", file=sys.stderr)
                        audio_failures.append((notebook_title, e))
                        no_audio.add(topic)
                else:
                    print(f"[{topic}] No usable sources; skipping audio generation.")
                    no_audio.add(topic)

                processed.extend(articles)
                blocked.extend(topic_blocked)
            except Exception as e:
                # 片方のトピックが失敗しても、もう片方のラジオは配信する
                print(f"Error processing topic '{topic}': {redact(e)}", file=sys.stderr)
                failures.append((notebook_title, e))

        # 4. 通知の送信（取り込めなかった記事は「生成開始」の一覧に載せず、正直に別枠で報告）
        blocked_urls = {url for url, _title in blocked}
        listed = [a for a in processed if a['link'] not in blocked_urls]
        if listed or blocked:
            send_notification(webhook_url, listed, blocked, notebooks, no_audio)

        # 5. 状態保存（処理できたトピックの記事の分だけ既読を進める。失敗分は次回に持ち越す）
        advance_state(state, feed_results, processed)
        save_state(state)

        # 6. メンテナンス（古いラジオの削除・停滞フィード検知。失敗しても本体は成功扱い）
        run_maintenance(config, state, webhook_url)

        if failures or audio_failures:
            for title, e in failures:
                send_error_notification(webhook_url, describe_failure(e), title)
            for title, e in audio_failures:
                send_error_notification(webhook_url, f"{AUDIO_FAILED_NOTE}\n\n{describe_failure(e)}", title)
            sys.exit(1)
        print("Batch process completed successfully.")

    except Exception as e:
        import traceback

        print(f"Unexpected error: {redact(traceback.format_exc())}", file=sys.stderr)
        send_error_notification(webhook_url, describe_failure(e), notebook_title)
        sys.exit(1)


if __name__ == '__main__':
    main()
