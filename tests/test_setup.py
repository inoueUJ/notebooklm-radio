"""scripts/setup.py の純粋な部分だけをテストする。外部コマンドは呼ばない。"""

import datetime
import shutil
import subprocess

from scripts import setup as wizard


def test_repo_from_remote_handles_every_github_url_form():
    assert wizard.repo_from_remote('git@github.com:inoueUJ/notebooklm-radio.git') == 'inoueUJ/notebooklm-radio'
    assert wizard.repo_from_remote('https://github.com/inoueUJ/notebooklm-radio') == 'inoueUJ/notebooklm-radio'
    assert wizard.repo_from_remote('https://github.com/inoueUJ/notebooklm-radio.git\n') == 'inoueUJ/notebooklm-radio'
    assert wizard.repo_from_remote('ssh://git@github.com/inoueUJ/notebooklm-radio/') == 'inoueUJ/notebooklm-radio'
    assert wizard.repo_from_remote('https://gitlab.com/x/y.git') is None


def test_pinned_version_is_read_from_the_workflow():
    assert wizard.pinned_notebooklm_version('uv tool install "notebooklm-py[browser]==0.8.0"') == '0.8.0'
    assert wizard.pinned_notebooklm_version('pip install notebooklm-py') is None
    # 実際のワークフローもピン留めされている（CI とローカルのバージョンを揃えるため）
    assert wizard.pinned_notebooklm_version(wizard.WORKFLOW_PATH.read_text(encoding='utf-8'))


def test_doctor_reports_missing_tools_without_running_anything(monkeypatch, tmp_path):
    """何も入っていないマシンでも doctor は落ちず、直し方を示す。"""
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
