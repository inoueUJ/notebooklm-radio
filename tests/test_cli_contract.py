"""notebooklm CLI との契約テスト。radio_batch.py などが渡すオプションが、固定した版の CLI にあるか。

テストの大半は CLI を差し替えて動くので、上流がオプションを消したり改名したりしても気付けない。
ここは本物の CLI の `--help` だけを見る(認証もネットワークも使わない)。CI では
requirements-notebooklm.txt の版を入れてから走るので、Dependabot の更新 PR がそのまま検査になる。
手元で CLI が無いか版が違うときは飛ばす。
"""

import os
import re
import shutil
import subprocess

import pytest

import radio_batch as rb
from scripts import setup as wizard

# radio_batch.py / scripts/weekly_check.py / scripts/setup.py が渡すオプション。呼び出しを足したらここも足す
USED_OPTIONS = {
    ('list',): ['--json'],
    ('create',): ['--json'],
    ('delete',): ['-n', '-y', '--json'],
    ('source', 'add'): ['--type', '--title', '--notebook', '--json'],
    ('source', 'wait'): ['--notebook'],
    ('source', 'list'): ['--notebook', '--json'],
    ('source', 'delete'): ['-y', '--notebook', '--json'],
    ('source', 'add-research'): ['--mode', '--no-wait', '--notebook', '--prompt-file', '--json'],
    ('research', 'wait'): ['-n', '--timeout', '--json'],
    ('generate', 'audio'): ['--no-wait', '--notebook', '--language', '--format', '--length', '-s', '--json'],
    ('auth', 'check'): ['--test', '--passive', '--json'],
    ('login',): ['--fresh'],
}


def _cli_or_skip():
    if not shutil.which('notebooklm'):
        if os.environ.get('CI'):
            pytest.fail('CI must install the notebooklm CLI before the tests (requirements-notebooklm.txt)')
        pytest.skip('notebooklm CLI is not installed')
    installed = wizard.installed_cli_version()
    pinned = wizard.pinned_version()
    if installed != pinned:
        if os.environ.get('CI'):
            pytest.fail(f'CI installed notebooklm-py {installed}, but requirements-notebooklm.txt pins {pinned}')
        pytest.skip(f'local notebooklm CLI is {installed}, the pin is {pinned}')


def _help(command, tmp_path):
    # NOTEBOOKLM_HOME を空の一時ディレクトリにして、手元のプロファイルには一切触れない
    env = {**os.environ, 'NOTEBOOKLM_HOME': str(tmp_path), 'COLUMNS': '300', 'NO_COLOR': '1'}
    result = subprocess.run(['notebooklm', *command, '--help'], capture_output=True, text=True, env=env, timeout=60)
    assert result.returncode == 0, f"`notebooklm {' '.join(command)} --help` failed: {result.stderr[-500:]}"
    return result.stdout


def _has_option(help_text, option):
    return re.search(rf'(?<![\w-]){re.escape(option)}(?![\w-])', help_text) is not None


@pytest.mark.parametrize('command', sorted(USED_OPTIONS), ids=' '.join)
def test_every_option_we_pass_still_exists(command, tmp_path):
    _cli_or_skip()
    help_text = _help(command, tmp_path)
    missing = [opt for opt in USED_OPTIONS[command] if not _has_option(help_text, opt)]
    assert not missing, f"`notebooklm {' '.join(command)}` no longer has {missing}"


def test_config_enums_are_accepted_by_the_cli(tmp_path):
    """config.yaml で許している音声の形式・長さ、テキスト投入、Deep Research の mode を CLI も受け付ける。"""
    _cli_or_skip()
    audio = _help(('generate', 'audio'), tmp_path)
    for value in rb.AUDIO_FORMATS | rb.AUDIO_LENGTHS:
        assert value in audio, f'generate audio no longer accepts {value!r}'
    assert 'text' in _help(('source', 'add'), tmp_path)
    assert 'deep' in _help(('source', 'add-research'), tmp_path)
