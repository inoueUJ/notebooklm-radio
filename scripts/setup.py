#!/usr/bin/env python3
"""notebooklm-radio setup wizard.

    python3 scripts/setup.py            # first-time setup (doctor → install → login → secrets → first run)
    python3 scripts/setup.py doctor     # read-only: what is installed, logged in, configured
    python3 scripts/setup.py renew      # credential renewal (CLI version → login → check → secret → run, watched)
    python3 scripts/setup.py --lang en  # messages in English (default: Japanese)

On Windows, type `python` instead of `python3`.

Standard library only, so it runs before anything is installed. Works on macOS, Linux and
Windows: the credential file is streamed to `gh secret set` through stdin, never through a
shell redirection, and its contents are never printed.

What it will and won't do:
- It runs the interactive Google login (`notebooklm -p ci login --fresh`) in *your* terminal
  and browser; the session cookie goes straight from that file into the repository secret.
- Before uploading, it checks the new credential with a read-only probe
  (`notebooklm -p ci auth check --test --passive`), printing only which checks failed.
- It never reads the credential file's contents, never sends anything anywhere except via
  the `gh` CLI to your own repository, and never touches NotebookLM otherwise.
- `doctor` changes nothing.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The notebooklm-py pin. CI installs from it and Dependabot bumps it, so the local CLI follows it too.
PIN_PATH = ROOT / 'requirements-notebooklm.txt'
WORKFLOW_FILE = 'rss-radio.yml'
PROFILE_ROOT = Path.home() / '.notebooklm' / 'profiles'
AUTH_SECRET = 'NOTEBOOKLM_AUTH_JSON'
WEBHOOK_SECRET = 'NOTIFY_WEBHOOK_URL'
AUTH_DATE_VARIABLE = 'NOTEBOOKLM_AUTH_UPDATED'  # read by the (future) expiry canary
CREDENTIAL_WARN_DAYS = 21  # sessions have lasted ~3.5 weeks; warn before that
MIN_PYTHON = (3, 10)
# How to run this script in the commands we print: macOS and most Linux have no `python`.
PY = 'python' if os.name == 'nt' else 'python3'
# `notebooklm auth check --json` check names. Only these are ever echoed back.
AUTH_CHECK_NAMES = ('storage_exists', 'json_valid', 'cookies_present', 'sid_cookie', 'token_fetch')

OK, BAD, WARN, SKIP = '✓', '✗', '!', '-'

# Message language, set from --lang in main(). Japanese by default: the locale is no guide
# (a Japanese macOS often runs its terminal with LANG=en_US.UTF-8).
LANG = 'ja'


# --- small helpers -----------------------------------------------------------------------


def t(ja: str, en: str) -> str:
    """The message in the current language."""
    return en if LANG == 'en' else ja


def parse_lang(argv: list[str] | None) -> str:
    """Read --lang before the real parser is built, so its help text is localized too."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument('--lang', default='ja')
    known, _rest = pre.parse_known_args(argv)
    return 'en' if known.lang == 'en' else 'ja'


def display_width(text: str) -> int:
    """Terminal columns: East Asian wide and fullwidth characters take two."""
    return sum(2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1 for ch in text)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command, capturing output as text. Never raises on non-zero exit."""
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def pinned_notebooklm_version(text: str) -> str | None:
    """The version in a `notebooklm-py[browser]==X` pin."""
    match = re.search(r'notebooklm-py\[browser\]==([\w.]+)', text)
    return match.group(1) if match else None


def pinned_version() -> str | None:
    """The notebooklm-py version CI installs (requirements-notebooklm.txt)."""
    return pinned_notebooklm_version(PIN_PATH.read_text(encoding='utf-8')) if PIN_PATH.exists() else None


def installed_cli_version() -> str | None:
    """The local notebooklm CLI's version, or None when it is not installed."""
    if not shutil.which('notebooklm'):
        return None
    match = re.search(r'version\s+([\w.]+)', run(['notebooklm', '--version']).stdout)
    return match.group(1) if match else None


def repo_from_remote(url: str) -> str | None:
    """'owner/repo' from any GitHub remote URL form."""
    url = url.strip()
    match = re.search(r'github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$', url)
    return f'{match.group(1)}/{match.group(2)}' if match else None


def detect_repo() -> str | None:
    if not shutil.which('git'):
        return None
    result = run(['git', 'remote', 'get-url', 'origin'], cwd=ROOT)
    return repo_from_remote(result.stdout) if result.returncode == 0 else None


def storage_state_path(profile: str) -> Path:
    return PROFILE_ROOT / profile / 'storage_state.json'


def age_days(path: Path) -> float | None:
    try:
        mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime, tz=datetime.UTC)
    except OSError:
        return None
    return (datetime.datetime.now(tz=datetime.UTC) - mtime).total_seconds() / 86400


def gh_ok() -> bool:
    return bool(shutil.which('gh')) and run(['gh', 'auth', 'status']).returncode == 0


def gh_secret_names(repo: str) -> set[str] | None:
    result = run(['gh', 'secret', 'list', '-R', repo])
    if result.returncode != 0:
        return None
    return {line.split()[0] for line in result.stdout.splitlines() if line.strip()}


def confirm(question: str, default: bool, assume_yes: bool) -> bool:
    if assume_yes:
        return default
    suffix = '[Y/n]' if default else '[y/N]'
    try:
        answer = input(f'{question} {suffix} ').strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ('y', 'yes')


def say(text: str = '') -> None:
    print(text, flush=True)


# --- doctor --------------------------------------------------------------------------------


@dataclass
class Check:
    status: str
    name: str
    detail: str = ''
    fix: str = ''


def doctor(repo: str | None, profile: str) -> list[Check]:
    checks: list[Check] = []

    version = sys.version.split()[0]
    if sys.version_info >= MIN_PYTHON:
        checks.append(Check(OK, 'Python', version))
    else:
        required = f'{MIN_PYTHON[0]}.{MIN_PYTHON[1]}'
        checks.append(
            Check(BAD, 'Python', version, t(f'Python {required} 以上が必要', f'Python {required}+ is required'))
        )

    repo_name = t('リポジトリ', 'Repository')
    if repo:
        checks.append(Check(OK, repo_name, repo))
    else:
        checks.append(
            Check(
                BAD,
                repo_name,
                t('origin という名前の GitHub リモートが無い', 'no GitHub remote named origin'),
                t(
                    '自分のリポジトリのクローンの中で実行するか、--repo OWNER/NAME を指定する',
                    'Run this inside a clone of your repo, or pass --repo OWNER/NAME',
                ),
            )
        )

    pinned = pinned_version()
    checks.append(
        Check(
            OK if pinned else BAD,
            t('固定バージョン', 'Pinned version'),
            t(f'notebooklm-py {pinned}（{PIN_PATH.name}）', f'notebooklm-py {pinned} ({PIN_PATH.name})')
            if pinned
            else t(
                f'{PIN_PATH.name} が無いか、notebooklm-py のバージョンが書かれていない',
                f'{PIN_PATH.name} not found or has no notebooklm-py pin',
            ),
        )
    )

    not_installed = t('インストールされていない', 'not installed')
    if shutil.which('uv'):
        checks.append(Check(OK, 'uv', run(['uv', '--version']).stdout.strip()))
    else:
        checks.append(Check(BAD, 'uv', not_installed, 'https://docs.astral.sh/uv/getting-started/installation/'))

    if not shutil.which('gh'):
        checks.append(
            Check(
                BAD,
                'GitHub CLI',
                not_installed,
                t('https://cli.github.com/ から入れて、gh auth login', 'https://cli.github.com/ then: gh auth login'),
            )
        )
    elif gh_ok():
        checks.append(Check(OK, 'GitHub CLI', t('インストール済み・ログイン済み', 'installed and logged in')))
    else:
        checks.append(Check(BAD, 'GitHub CLI', t('ログインしていない', 'not logged in'), 'gh auth login'))

    if shutil.which('notebooklm'):
        version = run(['notebooklm', '--version']).stdout.strip()
        installed = re.search(r'version\s+([\w.]+)', version)
        if pinned and installed and installed.group(1) != pinned:
            checks.append(
                Check(
                    WARN,
                    'notebooklm CLI',
                    t(
                        f'{installed.group(1)} が入っているが、CI は {pinned}',
                        f'{installed.group(1)} installed, CI uses {pinned}',
                    ),
                    f'uv tool install --force "notebooklm-py[browser]=={pinned}"',
                )
            )
        else:
            checks.append(Check(OK, 'notebooklm CLI', version or t('インストール済み', 'installed')))
    else:
        install = f'uv tool install "notebooklm-py[browser]=={pinned or "<version>"}"'
        checks.append(
            Check(
                BAD,
                'notebooklm CLI',
                not_installed,
                t(f'{install}（このウィザードでもできる）', f'{install}  (this wizard can do it)'),
            )
        )

    login_name = t(f'ログイン（{profile} プロファイル）', f'Login ({profile} profile)')
    credential = storage_state_path(profile)
    days = age_days(credential)
    if days is None:
        login_cmd = f'notebooklm -p {profile} login --fresh'
        checks.append(
            Check(
                BAD,
                login_name,
                t('認証ファイルがまだ無い', 'no credential file yet'),
                t(f'{login_cmd}（このウィザードでもできる）', f'{login_cmd}  (this wizard can do it)'),
            )
        )
    elif days > CREDENTIAL_WARN_DAYS:
        checks.append(
            Check(
                WARN,
                login_name,
                t(f'{days:.0f} 日前', f'{days:.0f} days old'),
                t(
                    f'セッションは約 3.5 週で切れてきた。{PY} scripts/setup.py renew で更新する',
                    f'sessions have expired at ~3.5 weeks; run: {PY} scripts/setup.py renew',
                ),
            )
        )
    else:
        checks.append(Check(OK, login_name, t(f'{days:.0f} 日前', f'{days:.0f} days old')))

    secrets_name = t('secret', 'Secrets')
    if repo and shutil.which('gh') and gh_ok():
        names = gh_secret_names(repo)
        if names is None:
            checks.append(
                Check(
                    WARN,
                    secrets_name,
                    t('一覧を取得できない（管理者権限が無い？）', 'could not list (no admin access?)'),
                    t('ウィザードからの設定はできる', 'the wizard can still set them'),
                )
            )
        else:
            for name in (AUTH_SECRET, WEBHOOK_SECRET):
                secret_name = t(f'secret {name}', f'Secret {name}')
                if name in names:
                    checks.append(Check(OK, secret_name, t('設定済み', 'set')))
                elif name == WEBHOOK_SECRET:
                    checks.append(
                        Check(
                            WARN,
                            secret_name,
                            t('未設定', 'not set'),
                            t(
                                '無いとエラーも含めて通知が一切届かない',
                                'without it you get no notifications, including errors',
                            ),
                        )
                    )
                else:
                    checks.append(
                        Check(
                            BAD,
                            secret_name,
                            t('未設定', 'not set'),
                            t('上のログインからウィザードが設定する', 'the wizard sets it from the login above'),
                        )
                    )
    else:
        checks.append(
            Check(
                SKIP,
                secrets_name,
                t('確認していない（リポジトリと gh のログインが必要）', 'skipped (needs repo + gh login)'),
            )
        )

    result = run([sys.executable, str(ROOT / 'radio_batch.py'), '--check-config'], cwd=ROOT)
    if result.returncode == 0:
        checks.append(
            Check(
                OK,
                'config.yaml',
                result.stdout.strip().splitlines()[-1] if result.stdout.strip() else t('問題なし', 'valid'),
            )
        )
    elif 'ModuleNotFoundError' in (result.stderr or ''):
        # The batch's own dependencies (feedparser/httpx/yaml) are only needed in CI, and CI
        # validates config.yaml before every run — don't make a local install a prerequisite.
        checks.append(
            Check(
                SKIP,
                'config.yaml',
                t(
                    'ここでは確認していない（Python パッケージが無い）。CI が毎回の実行前に確認する',
                    'not checked here (missing Python packages); CI validates it before every run',
                ),
                t(
                    '必要なら pip install -r requirements.txt の後で doctor をもう一度実行する',
                    'optional: pip install -r requirements.txt, then run doctor again',
                ),
            )
        )
    else:
        detail = (result.stderr or result.stdout).strip()
        lines = [line for line in detail.splitlines() if line.strip() and line.strip() != 'config.yaml NG']
        checks.append(
            Check(
                BAD,
                'config.yaml',
                ' / '.join(lines[:3]) if lines else t('不正', 'invalid'),
                t(
                    'config.yaml を直す（https://inoueuj.github.io/tech-feed-catalog/ で作り直してもいい）',
                    'fix config.yaml (or generate one at https://inoueuj.github.io/tech-feed-catalog/)',
                ),
            )
        )

    return checks


def print_checks(checks: list[Check]) -> bool:
    width = max(display_width(c.name) for c in checks)
    for c in checks:
        padding = ' ' * (width - display_width(c.name))
        say(f'  {c.status} {c.name}{padding}  {c.detail}')
        if c.fix and c.status in (BAD, WARN):
            say(f'    {"→"} {c.fix}')
    return all(c.status != BAD for c in checks)


# --- actions --------------------------------------------------------------------------------


def install_cli(pinned: str | None, assume_yes: bool) -> bool:
    if not shutil.which('uv'):
        say(
            t(
                '  uv が入っていないので、notebooklm CLI を自動ではインストールできません。',
                '  uv is not installed, so the notebooklm CLI cannot be installed automatically.',
            )
        )
        return False
    spec = f'notebooklm-py[browser]=={pinned}' if pinned else 'notebooklm-py[browser]'
    if not confirm(
        t(f'notebooklm CLI（{spec}）を uv でインストールしますか？', f'Install the notebooklm CLI ({spec}) with uv?'),
        True,
        assume_yes,
    ):
        return shutil.which('notebooklm') is not None
    say(f'  $ uv tool install --force "{spec}"')
    return subprocess.run(['uv', 'tool', 'install', '--force', spec]).returncode == 0


def match_cli_version(assume_yes: bool) -> bool:
    """Make the local notebooklm CLI the version CI pins, before logging in with it.

    The login is the local CLI's code, and it changes between versions: on 2026-10-05, 0.8.0
    reported "Already logged in" on a brand-new browser profile and saved an empty credential,
    while CI was about to move on. Renewing with an older CLI than CI's repeats bugs that are
    already fixed upstream.
    """
    pinned, installed = pinned_version(), installed_cli_version()
    if installed and (installed == pinned or not pinned):
        return True
    if installed:
        say(
            t(
                f'  手元の notebooklm CLI は {installed}、CI は {pinned} です。揃えてからログインします。',
                f'  The local notebooklm CLI is {installed}; CI uses {pinned}. Matching it before the login.',
            )
        )
    return install_cli(pinned, assume_yes)


def login(profile: str, assume_yes: bool) -> bool:
    """Interactive Google login into the CI profile. Opens a browser; nothing is captured here."""
    credential = storage_state_path(profile)
    days = age_days(credential)
    if (
        days is not None
        and days < 1
        and not confirm(
            t(
                f'今日すでに {profile} プロファイルでログインしています。ログインし直しますか？',
                f'A {profile} login from today already exists. Log in again?',
            ),
            False,
            assume_yes,
        )
    ):
        return True
    if not shutil.which('notebooklm'):
        say(
            t(
                '  notebooklm CLI が PATH にありません（インストール後に新しいターミナルを開くか、'
                '~/.local/bin を PATH に追加してください）。',
                '  notebooklm CLI is not on PATH (open a new terminal after installing, or add ~/.local/bin to PATH).',
            )
        )
        return False
    # Start from nothing. On 2026-10-05 two leftovers made renewals re-save a dead credential
    # (the CLI said "Already logged in" without any sign-in, and the run after the upload failed
    # like before): the profile's persistent browser, which still held the expired session, and
    # the previous storage_state.json. --fresh deletes the first but keeps the second, and with
    # that stale file present notebooklm-py 0.8.0 skipped the sign-in even on a brand-new browser
    # (reproduced with junk cookies; 0.8.4 waits for the sign-in). The file is about to be
    # replaced anyway, and the secret is untouched until verify_login passes.
    credential.unlink(missing_ok=True)
    say(f'  $ notebooklm -p {profile} login --fresh')
    say(
        t(
            '  ブラウザが開きます。NotebookLM を使える Google アカウントでログインしてください。',
            '  A browser window opens. Sign in with the Google account that has NotebookLM access.',
        )
    )
    say(
        t(
            '  ブラウザの古いセッションは消してから開くので、Google へのログインを毎回やり直します。',
            '  The browser\'s old session is cleared first, so you sign in to Google again every time (on purpose).',
        )
    )
    say(
        t(
            f'  {profile} プロファイルは CI 専用で、普段使いの default プロファイルとは分けておきます。',
            f'  Use the "{profile}" profile only for CI; your everyday "default" profile stays separate,',
        )
    )
    say(
        t(
            '  ローカルで notebooklm を使うたびに Cookie が更新され、CI に渡したコピーの寿命が縮むためです。',
            '  because every local notebooklm call rotates cookies and would shorten the CI copy\'s life.',
        )
    )
    result = subprocess.run(['notebooklm', '-p', profile, 'login', '--fresh'])
    if result.returncode != 0:
        say(t('  ログインが完了しませんでした。', '  Login did not complete.'))
        return False
    if not credential.exists():
        say(
            t(
                f'  ログインは終わりましたが、{credential} がありません。',
                f'  Login finished but {credential} does not exist.',
            )
        )
        return False
    return True


def verify_login(profile: str) -> bool:
    """Read-only check that the new credential works, before it goes into the secret.

    `auth check --test --passive` fetches a token with the saved cookies but never rotates
    cookies, runs a refresh command or writes storage (notebooklm-py's documented contract), so
    it does not shorten the life of the copy about to be uploaded. Only the names of failed
    checks are printed: the raw output could carry cookie values.
    """
    if not shutil.which('notebooklm'):
        say(t('  notebooklm CLI が PATH にありません。', '  notebooklm CLI is not on PATH.'))
        return False
    say(f'  $ notebooklm -p {profile} auth check --test --passive')
    result = run(['notebooklm', '-p', profile, 'auth', 'check', '--test', '--passive', '--json'])
    if result.returncode == 0:
        say(t('  ログインできていることを確認しました。', '  Credential verified.'))
        return True
    failed: list[str] = []
    try:
        payload = json.loads(result.stdout or '')
    except ValueError:
        payload = None
    checks = payload.get('checks') if isinstance(payload, dict) else None
    if isinstance(checks, dict):
        failed = [name for name in AUTH_CHECK_NAMES if checks.get(name) is False]
    say(
        t(
            '  保存した認証が使えません。ログインが最後まで終わっていない可能性があります。',
            '  The saved credential does not work; the login probably did not complete.',
        )
    )
    if failed:
        say(t(f'  通らなかった確認: {", ".join(failed)}', f'  Failed checks: {", ".join(failed)}'))
    say(
        t(
            '  secret は更新していません。もう一度実行して、ブラウザで Google へのログインを最後まで'
            '済ませてください（「ログインし直しますか？」と聞かれたら y）。',
            '  The secret was not updated. Run this again and finish the Google sign-in in the browser'
            ' (answer y if asked whether to log in again).',
        )
    )
    return False


def set_secret_from_file(repo: str, name: str, path: Path) -> bool:
    """`gh secret set NAME < file`, without a shell: works the same in PowerShell."""
    say(t(f'  $ gh secret set {name} -R {repo}（{path} から）', f'  $ gh secret set {name} -R {repo}  (from {path})'))
    with path.open('rb') as f:
        result = subprocess.run(['gh', 'secret', 'set', name, '-R', repo], stdin=f, capture_output=True, text=True)
    if result.returncode != 0:
        say(t(f'  失敗: {result.stderr.strip()}', f'  Failed: {result.stderr.strip()}'))
        return False
    return True


def set_secret_value(repo: str, name: str, value: str) -> bool:
    say(f'  $ gh secret set {name} -R {repo}')
    result = subprocess.run(['gh', 'secret', 'set', name, '-R', repo], input=value, capture_output=True, text=True)
    if result.returncode != 0:
        say(t(f'  失敗: {result.stderr.strip()}', f'  Failed: {result.stderr.strip()}'))
        return False
    return True


def record_auth_date(repo: str) -> None:
    """Repository variable with the date the credential was captured — lets a canary warn before expiry."""
    today = datetime.date.today().isoformat()
    result = run(['gh', 'variable', 'set', AUTH_DATE_VARIABLE, '--body', today, '-R', repo])
    if result.returncode == 0:
        say(
            t(
                f'  {AUTH_DATE_VARIABLE}={today} をリポジトリ変数に記録しました。',
                f'  Recorded {AUTH_DATE_VARIABLE}={today} (repository variable).',
            )
        )
    else:
        error = result.stderr.strip()
        say(
            t(
                f'  （{AUTH_DATE_VARIABLE} 変数を設定できませんでした: {error}。無くても動きます）',
                f'  (Could not set the {AUTH_DATE_VARIABLE} variable: {error} — not required.)',
            )
        )


def ask_webhook(assume_yes: bool, given: str | None) -> str | None:
    if given:
        return given.strip()
    if assume_yes:
        return None
    say(
        t(
            '  Slack の Incoming Webhook か Discord の Webhook の URL を貼り付けてください（入力は表示されません）。',
            '  Paste your Slack incoming-webhook or Discord webhook URL (input is hidden).',
        )
    )
    say(
        t(
            '  Slack: api.slack.com/apps → 自分のアプリ → Incoming Webhooks → Add New Webhook to Workspace',
            '  Slack: api.slack.com/apps → your app → Incoming Webhooks → Add New Webhook to Workspace.',
        )
    )
    say(
        t(
            '  Discord: チャンネルの編集 → 連携サービス → ウェブフック → 新しいウェブフック → ウェブフック URL をコピー',
            '  Discord: channel settings → Integrations → Webhooks → New Webhook → Copy Webhook URL.',
        )
    )
    say(
        t(
            '  空のまま Enter を押すと飛ばせます（その場合、エラーも含めて通知は届きません）。',
            '  Leave empty to skip (you will get no notifications, including error reports).',
        )
    )
    try:
        value = getpass.getpass('  Webhook URL: ').strip()
    except EOFError:
        return None
    if value and not value.startswith('https://'):
        say(
            t(
                '  Webhook の URL ではなさそうです（https:// で始まる必要があります）。飛ばします。',
                '  That does not look like a webhook URL (must start with https://). Skipping.',
            )
        )
        return None
    return value or None


def find_dispatched_run(repo: str, since: datetime.datetime, attempts: int = 20) -> dict | None:
    """The workflow_dispatch run created at or after `since` ({'databaseId', 'url'}), once GitHub lists it."""
    for attempt in range(attempts):
        filters = ['--workflow', WORKFLOW_FILE, '--event', 'workflow_dispatch', '--limit', '5']
        result = run(['gh', 'run', 'list', '-R', repo, *filters, '--json', 'databaseId,createdAt,url'])
        try:
            runs = json.loads(result.stdout or '[]') if result.returncode == 0 else []
        except ValueError:
            runs = []
        for item in runs:
            try:
                created = datetime.datetime.fromisoformat(item['createdAt'].replace('Z', '+00:00'))
            except (KeyError, TypeError, ValueError):
                continue
            # A few seconds of slack: our clock and GitHub's are not the same clock
            if created >= since - datetime.timedelta(seconds=10):
                return item
        if attempt + 1 < attempts:
            time.sleep(3)
    return None


def watch_run(repo: str, run_id: int, assume_yes: bool) -> bool | None:
    """Follow the run to the end and say how it went. None when the user chose not to wait."""
    if not confirm(
        t('終わるまでここで見届けますか？（3〜5 分ほど）', 'Wait here until it finishes (about 3–5 minutes)?'),
        True,
        assume_yes,
    ):
        say(
            t(
                f'  後で確認するには: gh run watch {run_id} -R {repo}',
                f'  To check later: gh run watch {run_id} -R {repo}',
            )
        )
        return None
    say(f'  $ gh run watch {run_id} -R {repo} --exit-status --compact')
    result = subprocess.run(
        ['gh', 'run', 'watch', str(run_id), '-R', repo, '--exit-status', '--compact', '--interval', '10']
    )
    if result.returncode == 0:
        say(
            t(
                '  成功しました。結果は Webhook に届きます。音声は NotebookLM の中で 20 分ほどかけて作られます。',
                '  It succeeded. The results arrive by webhook; the audio takes ~20 minutes to render in NotebookLM.',
            )
        )
        return True
    say(
        t(
            f'  失敗しました。原因は Webhook のエラー通知か、次のコマンドで見られます: gh run view {run_id} -R {repo} --log-failed',
            f'  It failed. The webhook error message has the cause, or run: gh run view {run_id} -R {repo} --log-failed',
        )
    )
    say(
        t(
            '  エラーに「CSRF token not found」や「Authentication expired」と出ていれば、認証がまだ通っていません。',
            '  "CSRF token not found" or "Authentication expired" in the error means the credential still does not work.',
        )
    )
    return False


def trigger_run(repo: str, assume_yes: bool) -> bool | None:
    """Start the workflow and, unless the user declines, watch it. False only when the run failed."""
    if not confirm(
        t(
            'ワークフローを今すぐ実行しますか？（今日のノートブックを作り、音声の生成を始めます）',
            'Run the workflow now (creates today\'s notebook and starts the audio)?',
        ),
        True,
        assume_yes,
    ):
        return None
    since = datetime.datetime.now(tz=datetime.UTC)
    say(f'  $ gh workflow run {WORKFLOW_FILE} -R {repo}')
    result = run(['gh', 'workflow', 'run', WORKFLOW_FILE, '-R', repo])
    if result.returncode != 0:
        say(t(f'  失敗: {result.stderr.strip()}', f'  Failed: {result.stderr.strip()}'))
        say(
            t(
                '  テンプレートから作ったばかりのリポジトリでワークフローが無効になっている場合は、'
                'Actions タブで一度有効にしてから実行し直してください。',
                '  If workflows are disabled on a fresh template repo, enable them once in the Actions tab and re-run.',
            )
        )
        return False
    # Saying "started" and stopping there left the outcome unknown: on 2026-10-05 the run after a
    # renewal failed exactly like before, and nobody could tell without opening the Actions tab.
    found = find_dispatched_run(repo, since)
    if not found:
        say(
            t(
                f'  開始しました。経過を見るには: https://github.com/{repo}/actions',
                f'  Started. Watch it at https://github.com/{repo}/actions',
            )
        )
        return None
    say(t(f'  開始しました: {found["url"]}', f'  Started: {found["url"]}'))
    return watch_run(repo, found['databaseId'], assume_yes)


# --- commands --------------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    say(t('notebooklm-radio doctor（確認のみ。何も変更しません）', 'notebooklm-radio doctor (read-only)'))
    ok = print_checks(doctor(args.repo or detect_repo(), args.profile))
    say()
    if ok:
        say(t('必要なものはそろっています。', 'Everything needed is in place.'))
    else:
        say(
            t(
                f'上の ✗ を直すか、{PY} scripts/setup.py を実行してください。',
                f'Fix the ✗ items above, or run: {PY} scripts/setup.py',
            )
        )
    return 0 if ok else 1


def cmd_init(args: argparse.Namespace) -> int:
    repo = args.repo or detect_repo()
    say(t('notebooklm-radio セットアップ', 'notebooklm-radio setup'))
    say()
    say(t('1/6  環境の確認', '1/6  Checking the machine'))
    checks = doctor(repo, args.profile)
    print_checks(checks)
    if not repo:
        say(
            t(
                '\nリポジトリが見つかりません。自分のリポジトリをクローンしてその中で実行するか、'
                '--repo OWNER/NAME を指定してください。',
                '\nNo repository detected. Clone your repo and run this inside it, or pass --repo OWNER/NAME.',
            )
        )
        return 1
    if not gh_ok():
        say(
            t(
                '\n先に GitHub CLI のインストールとログインが必要です: https://cli.github.com/ の後に gh auth login',
                '\nThe GitHub CLI must be installed and logged in first: https://cli.github.com/  then  gh auth login',
            )
        )
        return 1

    say()
    say('2/6  notebooklm CLI')
    if not install_cli(pinned_version(), args.yes):
        say(t('  インストールしてから、もう一度実行してください。', '  Install it, then run this again.'))
        return 1

    say()
    say(t('3/6  Google ログイン（CI 用プロファイル）', '3/6  Google login (CI profile)'))
    if not login(args.profile, args.yes) or not verify_login(args.profile):
        return 1

    say()
    say(t(f'4/6  リポジトリの secret {AUTH_SECRET}', f'4/6  Repository secret {AUTH_SECRET}'))
    if not set_secret_from_file(repo, AUTH_SECRET, storage_state_path(args.profile)):
        return 1
    record_auth_date(repo)

    say()
    say(t(f'5/6  リポジトリの secret {WEBHOOK_SECRET}', f'5/6  Repository secret {WEBHOOK_SECRET}'))
    names = gh_secret_names(repo) or set()
    if WEBHOOK_SECRET in names and not args.webhook:
        say(
            t(
                '  設定済みなのでそのままにします（置き換えるなら --webhook URL を指定）。',
                '  Already set; keeping it. (Pass --webhook URL to replace it.)',
            )
        )
    else:
        webhook = ask_webhook(args.yes, args.webhook)
        if webhook:
            if not set_secret_value(repo, WEBHOOK_SECRET, webhook):
                return 1
        else:
            say(
                t(
                    f'  飛ばしました。後から設定するなら: gh secret set {WEBHOOK_SECRET} -R {repo}',
                    f'  Skipped. You can set it later: gh secret set {WEBHOOK_SECRET} -R {repo}',
                )
            )

    say()
    say(t('6/6  初回の実行', '6/6  First run'))
    if args.no_run:
        say(t('  飛ばしました（--no-run）。', '  Skipped (--no-run).'))
    elif trigger_run(repo, args.yes) is False:
        return 1

    say()
    say(t('完了しました。この後の流れ:', 'Done. What happens next:'))
    say(
        t(
            '  - 数分以内に、今日の記事一覧とノートブックへのリンクが Webhook に届きます。',
            '  - Within a few minutes you get a webhook message with today\'s articles and a link to the notebook.',
        )
    )
    say(
        t(
            '  - 音声は NotebookLM の中で 20 分ほどかけて作られます（通知は「開始した」で、「完了した」ではありません）。',
            '  - The audio takes ~20 minutes to render inside NotebookLM (the notification says "started", not "finished").',
        )
    )
    say(
        t(
            f'  - Google のセッションは約 3.5 週で切れます。エラー通知が来たら {PY} scripts/setup.py renew を実行してください。',
            f'  - Every ~3.5 weeks the Google session expires; when the error message arrives, run: {PY} scripts/setup.py renew',
        )
    )
    say(
        t(
            '  - フィードや話し方は config.yaml で変えられます（https://inoueuj.github.io/tech-feed-catalog/ で作り直すこともできます）。',
            '  - Change feeds or style in config.yaml (or rebuild it at https://inoueuj.github.io/tech-feed-catalog/).',
        )
    )
    return 0


def cmd_renew(args: argparse.Namespace) -> int:
    repo = args.repo or detect_repo()
    say(t('notebooklm-radio 認証の更新', 'notebooklm-radio credential renewal'))
    if not repo:
        say(
            t(
                'リポジトリが見つかりません。自分のリポジトリのクローンの中で実行するか、--repo OWNER/NAME を指定してください。',
                'No repository detected. Run this inside a clone of your repo, or pass --repo OWNER/NAME.',
            )
        )
        return 1
    if not gh_ok():
        say(t('GitHub CLI にログインしてください: gh auth login', 'The GitHub CLI must be logged in: gh auth login'))
        return 1
    say()
    say('1/4  notebooklm CLI')
    if not match_cli_version(args.yes):
        return 1
    say()
    say(t('2/4  Google ログイン（CI 用プロファイル）', '2/4  Google login (CI profile)'))
    if not login(args.profile, args.yes) or not verify_login(args.profile):
        return 1
    say()
    say(t(f'3/4  リポジトリの secret {AUTH_SECRET}', f'3/4  Repository secret {AUTH_SECRET}'))
    if not set_secret_from_file(repo, AUTH_SECRET, storage_state_path(args.profile)):
        return 1
    record_auth_date(repo)
    say()
    say(t('4/4  再実行', '4/4  Re-run'))
    if args.no_run:
        say(t('  飛ばしました（--no-run）。', '  Skipped (--no-run).'))
        return 0
    return 1 if trigger_run(repo, args.yes) is False else 0


def main(argv: list[str] | None = None) -> int:
    global LANG
    LANG = parse_lang(argv)
    parser = argparse.ArgumentParser(
        description=t('notebooklm-radio セットアップウィザード', 'notebooklm-radio setup wizard'),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        'command',
        nargs='?',
        choices=['init', 'doctor', 'renew'],
        default='init',
        help=t(
            'init（既定）: 初回セットアップ / doctor: 確認のみ / renew: 認証の更新',
            'init (default): first-time setup; doctor: read-only checks; renew: credential renewal',
        ),
    )
    parser.add_argument(
        '--repo',
        help=t('OWNER/NAME（既定: このクローンの origin）', 'OWNER/NAME (default: the origin remote of this clone)'),
    )
    parser.add_argument(
        '--profile',
        default='ci',
        help=t(
            'CI 用の notebooklm ログインプロファイル（既定: ci）', 'notebooklm login profile used for CI (default: ci)'
        ),
    )
    parser.add_argument(
        '--webhook',
        help=t(
            'NOTIFY_WEBHOOK_URL に保存する Webhook URL（省略すると入力を求める。入力は表示しない）',
            'webhook URL to store as NOTIFY_WEBHOOK_URL (otherwise asked interactively, hidden)',
        ),
    )
    parser.add_argument(
        '--yes', '-y', action='store_true', help=t('質問せず既定の答えで進める', 'accept defaults without asking')
    )
    parser.add_argument(
        '--no-run',
        action='store_true',
        help=t('最後にワークフローを実行しない', 'do not trigger the workflow at the end'),
    )
    parser.add_argument(
        '--lang', choices=['ja', 'en'], default='ja', help=t('表示言語（既定: ja）', 'message language (default: ja)')
    )
    args = parser.parse_args(argv)
    if os.name == 'nt':
        # Emoji status marks need a UTF-8 console; fall back to ASCII if the terminal cannot show them.
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError):
            pass
    return {'init': cmd_init, 'doctor': cmd_doctor, 'renew': cmd_renew}[args.command](args)


if __name__ == '__main__':
    sys.exit(main())
