"""scripts/setup.py の純粋な部分だけをテストする。外部コマンドは呼ばない。"""

import argparse
import datetime
import json
import os
import shutil
import subprocess

import pytest

from scripts import setup as wizard


def test_repo_from_remote_handles_every_github_url_form():
    assert wizard.repo_from_remote('git@github.com:inoueUJ/notebooklm-radio.git') == 'inoueUJ/notebooklm-radio'
    assert wizard.repo_from_remote('https://github.com/inoueUJ/notebooklm-radio') == 'inoueUJ/notebooklm-radio'
    assert wizard.repo_from_remote('https://github.com/inoueUJ/notebooklm-radio.git\n') == 'inoueUJ/notebooklm-radio'
    assert wizard.repo_from_remote('ssh://git@github.com/inoueUJ/notebooklm-radio/') == 'inoueUJ/notebooklm-radio'
    assert wizard.repo_from_remote('https://gitlab.com/x/y.git') is None


def test_pinned_version_is_read_from_the_pin_file():
    assert wizard.pinned_notebooklm_version('notebooklm-py[browser]==0.8.4\n') == '0.8.4'
    assert wizard.pinned_notebooklm_version('pip install notebooklm-py') is None
    # 実際の固定ファイルにも版が書いてある（CI とローカルのバージョンを揃えるため）
    assert wizard.pinned_version()


def test_workflows_install_the_cli_from_the_pin_file():
    """版を書くのは requirements-notebooklm.txt の 1 か所だけ。ワークフローに直書きが残ると
    Dependabot が上げても CI は古い版のままになる。"""
    workflows = sorted((wizard.ROOT / '.github' / 'workflows').glob('*.yml'))
    installs = [w.name for w in workflows if 'uv tool install' in w.read_text(encoding='utf-8')]
    assert {'rss-radio.yml', 'weekly-check.yml', 'ci.yml'} <= set(installs)
    for w in workflows:
        text = w.read_text(encoding='utf-8')
        assert 'notebooklm-py[browser]==' not in text, f'{w.name} hard-codes the notebooklm-py version'
        if 'uv tool install' in text:
            assert 'requirements-notebooklm.txt' in text, w.name


def test_doctor_reports_missing_tools_without_running_anything(monkeypatch, tmp_path):
    """何も入っていないマシンでも doctor は落ちず、直し方を示す。"""
    monkeypatch.setattr(wizard, 'LANG', 'en')  # 項目名で引くので英語表示に固定する
    monkeypatch.setattr(shutil, 'which', lambda name: None)
    monkeypatch.setattr(wizard, 'PROFILE_ROOT', tmp_path)

    def no_subprocess(*a, **k):
        raise AssertionError('doctor must not run commands when the tools are missing')

    monkeypatch.setattr(subprocess, 'run', no_subprocess)
    # config.yaml の検査だけは自分自身（sys.executable）を呼ぶので、そこは差し替える
    monkeypatch.setattr(
        wizard,
        'run',
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout='config.yaml OK: 3 feed(s)\n', stderr=''),
    )
    checks = wizard.doctor(repo=None, profile='ci')
    by_name = {c.name: c for c in checks}
    assert by_name['Repository'].status == wizard.BAD and '--repo' in by_name['Repository'].fix
    assert by_name['uv'].status == wizard.BAD
    assert by_name['GitHub CLI'].status == wizard.BAD and 'gh auth login' in by_name['GitHub CLI'].fix
    assert by_name['notebooklm CLI'].status == wizard.BAD and 'uv tool install' in by_name['notebooklm CLI'].fix
    assert by_name['Login (ci profile)'].status == wizard.BAD
    assert by_name['Secrets'].status == wizard.SKIP
    assert by_name['config.yaml'].status == wizard.OK
    assert wizard.print_checks(checks) is False


def test_doctor_warns_when_the_credential_is_older_than_three_weeks(monkeypatch, tmp_path):
    monkeypatch.setattr(wizard, 'LANG', 'en')
    monkeypatch.setattr(shutil, 'which', lambda name: None)
    monkeypatch.setattr(wizard, 'PROFILE_ROOT', tmp_path)
    monkeypatch.setattr(wizard, 'run', lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stdout='', stderr='NG'))
    credential = tmp_path / 'ci' / 'storage_state.json'
    credential.parent.mkdir(parents=True)
    credential.write_text('{}')
    old = (datetime.datetime.now() - datetime.timedelta(days=25)).timestamp()
    import os

    os.utime(credential, (old, old))
    checks = {c.name: c for c in wizard.doctor(repo='o/r', profile='ci')}
    assert checks['Login (ci profile)'].status == wizard.WARN and 'renew' in checks['Login (ci profile)'].fix
    assert checks['config.yaml'].status == wizard.BAD


# --- 表示言語 ----------------------------------------------------------------------------


def test_messages_are_japanese_by_default_and_english_with_lang_en(monkeypatch, capsys):
    monkeypatch.setattr(wizard, 'LANG', 'ja')
    assert wizard.t('日本語', 'English') == '日本語'
    monkeypatch.setattr(wizard, 'LANG', 'en')
    assert wizard.t('日本語', 'English') == 'English'

    # 端末の LANG（日本語の macOS でも en_US.UTF-8 のことが多い）ではなく --lang だけで決める
    assert wizard.parse_lang(['renew']) == 'ja'
    assert wizard.parse_lang(['renew', '--lang', 'en']) == 'en'
    assert wizard.parse_lang(['--lang=en', 'doctor']) == 'en'

    # 引数の説明（--help）も切り替わる。main() が LANG を書き換えるので monkeypatch で元に戻す
    monkeypatch.setattr(wizard, 'LANG', 'ja')
    with pytest.raises(SystemExit):
        wizard.main(['--help'])
    assert '初回セットアップ' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        wizard.main(['--help', '--lang', 'en'])
    assert 'first-time setup' in capsys.readouterr().out


def test_check_columns_line_up_with_japanese_names(capsys):
    """日本語は 1 文字で 2 桁使うので、文字数で揃えると詳細の列がずれる。"""
    checks = [wizard.Check(wizard.OK, 'uv', 'DETAIL-A'), wizard.Check(wizard.OK, 'リポジトリ', 'DETAIL-B')]
    wizard.print_checks(checks)
    lines = capsys.readouterr().out.splitlines()
    columns = [
        wizard.display_width(line[: line.index(marker)])
        for line, marker in zip(lines, ('DETAIL-A', 'DETAIL-B'), strict=True)
    ]
    assert columns[0] == columns[1]


# --- ログインと認証の確認 -----------------------------------------------------------------


def test_login_starts_from_a_fresh_browser_and_no_stale_credential(monkeypatch, tmp_path):
    """前回のブラウザのセッションも、前回の storage_state.json も残さない。

    2026-10-05、どちらかが残っていると CLI はログイン画面を出さずに「ログイン済み」と判定し、
    使えない Cookie を保存した（--fresh はブラウザしか消さない）。更新した secret も同じエラーで落ちた。
    """
    monkeypatch.setattr(wizard, 'PROFILE_ROOT', tmp_path)
    monkeypatch.setattr(shutil, 'which', lambda name: f'/usr/local/bin/{name}')
    credential = tmp_path / 'ci' / 'storage_state.json'
    credential.parent.mkdir(parents=True)
    credential.write_text('{"stale": true}')
    old = (datetime.datetime.now() - datetime.timedelta(days=40)).timestamp()
    os.utime(credential, (old, old))
    calls = []

    def fake_login(cmd, *a, **k):
        calls.append((cmd, credential.exists()))
        credential.write_text('{}')
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, 'run', fake_login)
    assert wizard.login('ci', assume_yes=True)
    assert calls == [(['notebooklm', '-p', 'ci', 'login', '--fresh'], False)]


def test_renew_does_not_upload_a_credential_that_fails_the_check(monkeypatch):
    monkeypatch.setattr(wizard, 'detect_repo', lambda: 'o/r')
    monkeypatch.setattr(wizard, 'gh_ok', lambda: True)
    monkeypatch.setattr(wizard, 'match_cli_version', lambda assume_yes: True)
    monkeypatch.setattr(wizard, 'login', lambda profile, assume_yes: True)
    monkeypatch.setattr(wizard, 'verify_login', lambda profile: False)
    uploaded = []
    monkeypatch.setattr(wizard, 'set_secret_from_file', lambda *a: uploaded.append(a) or True)

    args = argparse.Namespace(repo=None, profile='ci', yes=True, no_run=True)
    assert wizard.cmd_renew(args) == 1
    assert uploaded == []


def test_verify_login_names_failed_checks_without_echoing_output(monkeypatch, capsys):
    """CLI の出力には Cookie が載りうるので、通らなかった確認の名前だけを出す。"""
    monkeypatch.setattr(shutil, 'which', lambda name: f'/usr/local/bin/{name}')
    secret = '__Secure-1PSID=SUPERSECRETCOOKIEVALUE'
    stdout = json.dumps(
        {
            'status': 'error',
            'checks': {'storage_exists': True, 'cookies_present': False, 'sid_cookie': False, 'token_fetch': None},
            'detail': secret,
        }
    )
    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout=stdout, stderr=secret)

    monkeypatch.setattr(wizard, 'run', fake_run)
    assert wizard.verify_login('ci') is False
    out = capsys.readouterr().out
    assert 'SUPERSECRETCOOKIEVALUE' not in out
    assert 'sid_cookie' in out and 'cookies_present' in out
    assert 'token_fetch' not in out  # None は「実行していない」で、失敗ではない
    # 読み取りだけの確認（--passive は Cookie を回さず、ファイルも書き換えない）
    assert calls == [['notebooklm', '-p', 'ci', 'auth', 'check', '--test', '--passive', '--json']]

    monkeypatch.setattr(wizard, 'run', lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout='{}', stderr=''))
    assert wizard.verify_login('ci') is True


def test_renew_matches_the_local_cli_to_the_pin_before_logging_in(monkeypatch):
    """ログインは手元の CLI のコード。CI より古い版のままだと、上流で直った不具合を繰り返す。"""
    monkeypatch.setattr(wizard, 'pinned_version', lambda: '0.8.4')
    installs = []
    monkeypatch.setattr(wizard, 'install_cli', lambda pinned, assume_yes: installs.append(pinned) or True)

    monkeypatch.setattr(wizard, 'installed_cli_version', lambda: '0.8.4')
    assert wizard.match_cli_version(assume_yes=True) and installs == []

    monkeypatch.setattr(wizard, 'installed_cli_version', lambda: '0.8.0')
    assert wizard.match_cli_version(assume_yes=True) and installs == ['0.8.4']

    monkeypatch.setattr(wizard, 'installed_cli_version', lambda: None)
    assert wizard.match_cli_version(assume_yes=True) and installs == ['0.8.4', '0.8.4']


def test_dispatched_run_is_the_one_created_after_the_dispatch(monkeypatch):
    since = datetime.datetime(2026, 10, 5, 11, 0, tzinfo=datetime.UTC)
    runs = [
        {'databaseId': 2, 'createdAt': '2026-10-05T11:00:03Z', 'url': 'https://x/2'},
        {'databaseId': 1, 'createdAt': '2026-10-05T10:00:00Z', 'url': 'https://x/1'},
    ]
    monkeypatch.setattr(wizard, 'run', lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, json.dumps(runs), ''))
    assert wizard.find_dispatched_run('o/r', since, attempts=1)['databaseId'] == 2

    # まだ一覧に出ていない（古い実行しかない）ときは None
    monkeypatch.setattr(wizard, 'run', lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, json.dumps(runs[1:]), ''))
    assert wizard.find_dispatched_run('o/r', since, attempts=1) is None


def test_renew_fails_when_the_watched_run_fails(monkeypatch, capsys):
    """「開始しました」で終わらせない。実行が失敗したら renew 自体を失敗にして、見方を示す。"""
    monkeypatch.setattr(wizard, 'detect_repo', lambda: 'o/r')
    monkeypatch.setattr(wizard, 'gh_ok', lambda: True)
    monkeypatch.setattr(wizard, 'match_cli_version', lambda assume_yes: True)
    monkeypatch.setattr(wizard, 'login', lambda profile, assume_yes: True)
    monkeypatch.setattr(wizard, 'verify_login', lambda profile: True)
    monkeypatch.setattr(wizard, 'set_secret_from_file', lambda *a: True)
    monkeypatch.setattr(wizard, 'record_auth_date', lambda repo: None)
    monkeypatch.setattr(wizard, 'run', lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, '', ''))
    monkeypatch.setattr(wizard, 'find_dispatched_run', lambda repo, since: {'databaseId': 7, 'url': 'https://x/7'})
    monkeypatch.setattr(subprocess, 'run', lambda cmd, **k: subprocess.CompletedProcess(cmd, 1))

    args = argparse.Namespace(repo=None, profile='ci', yes=True, no_run=False)
    assert wizard.cmd_renew(args) == 1
    out = capsys.readouterr().out
    assert 'gh run view 7 -R o/r --log-failed' in out
