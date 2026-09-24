#!/usr/bin/env python3
"""notebooklm-radio setup wizard.

    python scripts/setup.py            # first-time setup (doctor → install → login → secrets → first run)
    python scripts/setup.py doctor     # read-only: what is installed, logged in, configured
    python scripts/setup.py renew      # the ~3.5-week credential renewal (login → secret → optional run)

Standard library only, so it runs before anything is installed. Works on macOS, Linux and
Windows: the credential file is streamed to `gh secret set` through stdin, never through a
shell redirection, and its contents are never printed.

What it will and won't do:
- It runs the interactive Google login (`notebooklm -p ci login`) in *your* terminal and
  browser; the session cookie goes straight from that file into the repository secret.
- It never reads the credential file's contents, never sends anything anywhere except via
  the `gh` CLI to your own repository, and never touches NotebookLM itself.
- `doctor` changes nothing.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = ROOT / '.github' / 'workflows' / 'rss-radio.yml'
WORKFLOW_FILE = 'rss-radio.yml'
PROFILE_ROOT = Path.home() / '.notebooklm' / 'profiles'
AUTH_SECRET = 'NOTEBOOKLM_AUTH_JSON'
WEBHOOK_SECRET = 'NOTIFY_WEBHOOK_URL'
AUTH_DATE_VARIABLE = 'NOTEBOOKLM_AUTH_UPDATED'  # read by the (future) expiry canary
CREDENTIAL_WARN_DAYS = 21  # sessions have lasted ~3.5 weeks; warn before that
MIN_PYTHON = (3, 10)

OK, BAD, WARN, SKIP = '✓', '✗', '!', '-'


# --- small helpers -----------------------------------------------------------------------


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command, capturing output as text. Never raises on non-zero exit."""
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def pinned_notebooklm_version(workflow_text: str) -> str | None:
    """The notebooklm-py version CI installs, so the local install matches it."""
    match = re.search(r'notebooklm-py\[browser\]==([\w.]+)', workflow_text)
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

    if sys.version_info >= MIN_PYTHON:
        checks.append(Check(OK, 'Python', f'{sys.version.split()[0]}'))
    else:
        checks.append(
            Check(BAD, 'Python', f'{sys.version.split()[0]}', f'Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required')
        )

    if repo:
        checks.append(Check(OK, 'Repository', repo))
    else:
        checks.append(
            Check(
                BAD,
                'Repository',
                'no GitHub remote named origin',
                'Run this inside a clone of your repo, or pass --repo OWNER/NAME',
            )
        )

    workflow_text = WORKFLOW_PATH.read_text(encoding='utf-8') if WORKFLOW_PATH.exists() else ''
    pinned = pinned_notebooklm_version(workflow_text)
    checks.append(
        Check(
            OK if pinned else BAD,
            'Workflow',
            f'pins notebooklm-py {pinned}' if pinned else 'rss-radio.yml not found or has no pinned notebooklm-py',
        )
    )

    if shutil.which('uv'):
        checks.append(Check(OK, 'uv', run(['uv', '--version']).stdout.strip()))
    else:
        checks.append(Check(BAD, 'uv', 'not installed', 'https://docs.astral.sh/uv/getting-started/installation/'))

    if not shutil.which('gh'):
        checks.append(Check(BAD, 'GitHub CLI', 'not installed', 'https://cli.github.com/ then: gh auth login'))
    elif gh_ok():
        checks.append(Check(OK, 'GitHub CLI', 'installed and logged in'))
    else:
        checks.append(Check(BAD, 'GitHub CLI', 'not logged in', 'gh auth login'))

    if shutil.which('notebooklm'):
        version = run(['notebooklm', '--version']).stdout.strip()
        installed = re.search(r'version\s+([\w.]+)', version)
        if pinned and installed and installed.group(1) != pinned:
            checks.append(
                Check(
                    WARN,
                    'notebooklm CLI',
                    f'{installed.group(1)} installed, CI uses {pinned}',
                    f'uv tool install --force "notebooklm-py[browser]=={pinned}"',
                )
            )
        else:
            checks.append(Check(OK, 'notebooklm CLI', version or 'installed'))
    else:
        checks.append(
            Check(
                BAD,
                'notebooklm CLI',
                'not installed',
                f'uv tool install "notebooklm-py[browser]=={pinned or "<version>"}"  (this wizard can do it)',
            )
        )

    credential = storage_state_path(profile)
    days = age_days(credential)
    if days is None:
        checks.append(
            Check(
                BAD,
                f'Login ({profile} profile)',
                'no credential file yet',
                f'notebooklm -p {profile} login  (this wizard can do it)',
            )
        )
    elif days > CREDENTIAL_WARN_DAYS:
        checks.append(
            Check(
                WARN,
                f'Login ({profile} profile)',
                f'{days:.0f} days old',
                'sessions have expired at ~3.5 weeks; run: python scripts/setup.py renew',
            )
        )
    else:
        checks.append(Check(OK, f'Login ({profile} profile)', f'{days:.0f} days old'))

    if repo and shutil.which('gh') and gh_ok():
        names = gh_secret_names(repo)
        if names is None:
            checks.append(Check(WARN, 'Secrets', 'could not list (no admin access?)', 'the wizard can still set them'))
        else:
            for name in (AUTH_SECRET, WEBHOOK_SECRET):
                if name in names:
                    checks.append(Check(OK, f'Secret {name}', 'set'))
                elif name == WEBHOOK_SECRET:
                    checks.append(
                        Check(
                            WARN, f'Secret {name}', 'not set', 'without it you get no notifications, including errors'
                        )
                    )
                else:
                    checks.append(Check(BAD, f'Secret {name}', 'not set', 'the wizard sets it from the login above'))
    else:
        checks.append(Check(SKIP, 'Secrets', 'skipped (needs repo + gh login)'))

    result = run([sys.executable, str(ROOT / 'radio_batch.py'), '--check-config'], cwd=ROOT)
    if result.returncode == 0:
        checks.append(
            Check(OK, 'config.yaml', result.stdout.strip().splitlines()[-1] if result.stdout.strip() else 'valid')
        )
    elif 'ModuleNotFoundError' in (result.stderr or ''):
        # The batch's own dependencies (feedparser/httpx/yaml) are only needed in CI, and CI
        # validates config.yaml before every run — don't make a local install a prerequisite.
        checks.append(
            Check(
                SKIP,
                'config.yaml',
                'not checked here (missing Python packages); CI validates it before every run',
                'optional: pip install -r requirements.txt, then run doctor again',
            )
        )
    else:
        detail = (result.stderr or result.stdout).strip()
        lines = [line for line in detail.splitlines() if line.strip() and line.strip() != 'config.yaml NG']
        checks.append(
            Check(
                BAD,
                'config.yaml',
                ' / '.join(lines[:3]) if lines else 'invalid',
                'fix config.yaml (or generate one at https://inoueuj.github.io/tech-feed-catalog/)',
            )
        )

    return checks


def print_checks(checks: list[Check]) -> bool:
    width = max(len(c.name) for c in checks)
    for c in checks:
        say(f'  {c.status} {c.name.ljust(width)}  {c.detail}')
        if c.fix and c.status in (BAD, WARN):
            say(f'    {"→"} {c.fix}')
    return all(c.status != BAD for c in checks)


# --- actions --------------------------------------------------------------------------------


def install_cli(pinned: str | None, assume_yes: bool) -> bool:
    if not shutil.which('uv'):
        say('  uv is not installed, so the notebooklm CLI cannot be installed automatically.')
        return False
    spec = f'notebooklm-py[browser]=={pinned}' if pinned else 'notebooklm-py[browser]'
    if not confirm(f'Install the notebooklm CLI ({spec}) with uv?', True, assume_yes):
        return shutil.which('notebooklm') is not None
    say(f'  $ uv tool install --force "{spec}"')
    return subprocess.run(['uv', 'tool', 'install', '--force', spec]).returncode == 0


def login(profile: str, assume_yes: bool) -> bool:
    """Interactive Google login into the CI profile. Opens a browser; nothing is captured here."""
    credential = storage_state_path(profile)
    days = age_days(credential)
    if (
        days is not None
        and days < 1
        and not confirm(f'A {profile} login from today already exists. Log in again?', False, assume_yes)
    ):
        return True
    if not shutil.which('notebooklm'):
        say('  notebooklm CLI is not on PATH (open a new terminal after installing, or add ~/.local/bin to PATH).')
        return False
    say(f'  $ notebooklm -p {profile} login')
    say('  A browser window opens. Sign in with the Google account that has NotebookLM access.')
    say(f'  Use the "{profile}" profile only for CI; your everyday "default" profile stays separate,')
    say('  because every local notebooklm call rotates cookies and would shorten the CI copy\'s life.')
    result = subprocess.run(['notebooklm', '-p', profile, 'login'])
    if result.returncode != 0:
        say('  Login did not complete.')
        return False
    if not credential.exists():
        say(f'  Login finished but {credential} does not exist.')
        return False
    return True


def set_secret_from_file(repo: str, name: str, path: Path) -> bool:
    """`gh secret set NAME < file`, without a shell: works the same in PowerShell."""
    say(f'  $ gh secret set {name} -R {repo}  (from {path})')
    with path.open('rb') as f:
        result = subprocess.run(['gh', 'secret', 'set', name, '-R', repo], stdin=f, capture_output=True, text=True)
    if result.returncode != 0:
        say(f'  Failed: {result.stderr.strip()}')
        return False
    return True


def set_secret_value(repo: str, name: str, value: str) -> bool:
    say(f'  $ gh secret set {name} -R {repo}')
    result = subprocess.run(['gh', 'secret', 'set', name, '-R', repo], input=value, capture_output=True, text=True)
    if result.returncode != 0:
        say(f'  Failed: {result.stderr.strip()}')
        return False
    return True


def record_auth_date(repo: str) -> None:
    """Repository variable with the date the credential was captured — lets a canary warn before expiry."""
    today = datetime.date.today().isoformat()
    result = run(['gh', 'variable', 'set', AUTH_DATE_VARIABLE, '--body', today, '-R', repo])
    if result.returncode == 0:
        say(f'  Recorded {AUTH_DATE_VARIABLE}={today} (repository variable).')
    else:
        say(f'  (Could not set the {AUTH_DATE_VARIABLE} variable: {result.stderr.strip()} — not required.)')


def ask_webhook(assume_yes: bool, given: str | None) -> str | None:
    if given:
        return given.strip()
    if assume_yes:
        return None
    say('  Paste your Slack incoming-webhook or Discord webhook URL (input is hidden).')
    say('  Slack: api.slack.com/apps → your app → Incoming Webhooks → Add New Webhook to Workspace.')
    say('  Discord: channel settings → Integrations → Webhooks → New Webhook → Copy Webhook URL.')
    say('  Leave empty to skip (you will get no notifications, including error reports).')
    try:
        value = getpass.getpass('  Webhook URL: ').strip()
    except EOFError:
        return None
    if value and not value.startswith('https://'):
        say('  That does not look like a webhook URL (must start with https://). Skipping.')
        return None
    return value or None


def trigger_run(repo: str, assume_yes: bool) -> None:
    if not confirm('Run the workflow now (creates today\'s notebook and starts the audio)?', True, assume_yes):
        return
    say(f'  $ gh workflow run {WORKFLOW_FILE} -R {repo}')
    result = run(['gh', 'workflow', 'run', WORKFLOW_FILE, '-R', repo])
    if result.returncode != 0:
        say(f'  Failed: {result.stderr.strip()}')
        say('  If workflows are disabled on a fresh template repo, enable them once in the Actions tab and re-run.')
        return
    say(f'  Started. Watch it: gh run watch -R {repo}   or   https://github.com/{repo}/actions')


# --- commands --------------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    say('notebooklm-radio doctor (read-only)')
    ok = print_checks(doctor(args.repo or detect_repo(), args.profile))
    say()
    say('Everything needed is in place.' if ok else 'Fix the ✗ items above, or run: python scripts/setup.py')
    return 0 if ok else 1


def cmd_init(args: argparse.Namespace) -> int:
    repo = args.repo or detect_repo()
    say('notebooklm-radio setup')
    say()
    say('1/6  Checking the machine')
    checks = doctor(repo, args.profile)
    print_checks(checks)
    if not repo:
        say('\nNo repository detected. Clone your repo and run this inside it, or pass --repo OWNER/NAME.')
        return 1
    if not gh_ok():
        say('\nThe GitHub CLI must be installed and logged in first: https://cli.github.com/  then  gh auth login')
        return 1

    pinned = pinned_notebooklm_version(WORKFLOW_PATH.read_text(encoding='utf-8')) if WORKFLOW_PATH.exists() else None
    say()
    say('2/6  notebooklm CLI')
    if not install_cli(pinned, args.yes):
        say('  Install it, then run this again.')
        return 1

    say()
    say('3/6  Google login (CI profile)')
    if not login(args.profile, args.yes):
        return 1

    say()
    say(f'4/6  Repository secret {AUTH_SECRET}')
    if not set_secret_from_file(repo, AUTH_SECRET, storage_state_path(args.profile)):
        return 1
    record_auth_date(repo)

    say()
    say(f'5/6  Repository secret {WEBHOOK_SECRET}')
    names = gh_secret_names(repo) or set()
    if WEBHOOK_SECRET in names and not args.webhook:
        say('  Already set; keeping it. (Pass --webhook URL to replace it.)')
    else:
        webhook = ask_webhook(args.yes, args.webhook)
        if webhook:
            if not set_secret_value(repo, WEBHOOK_SECRET, webhook):
                return 1
        else:
            say('  Skipped. You can set it later: gh secret set NOTIFY_WEBHOOK_URL -R ' + repo)

    say()
    say('6/6  First run')
    if args.no_run:
        say('  Skipped (--no-run).')
    else:
        trigger_run(repo, args.yes)

    say()
    say('Done. What happens next:')
    say('  - Within a few minutes you get a webhook message with today\'s articles and a link to the notebook.')
    say(
        '  - The audio takes ~20 minutes to render inside NotebookLM (the notification says "started", not "finished").'
    )
    say(
        '  - Every ~3.5 weeks the Google session expires; when the error message arrives, run: python scripts/setup.py renew'
    )
    say('  - Change feeds or style in config.yaml (or rebuild it at https://inoueuj.github.io/tech-feed-catalog/).')
    return 0


def cmd_renew(args: argparse.Namespace) -> int:
    repo = args.repo or detect_repo()
    say('notebooklm-radio credential renewal')
    if not repo:
        say('No repository detected. Run this inside a clone of your repo, or pass --repo OWNER/NAME.')
        return 1
    if not gh_ok():
        say('The GitHub CLI must be logged in: gh auth login')
        return 1
    say()
    say('1/3  Google login (CI profile)')
    if not login(args.profile, args.yes):
        return 1
    say()
    say(f'2/3  Repository secret {AUTH_SECRET}')
    if not set_secret_from_file(repo, AUTH_SECRET, storage_state_path(args.profile)):
        return 1
    record_auth_date(repo)
    say()
    say('3/3  Re-run')
    if args.no_run:
        say('  Skipped (--no-run).')
    else:
        trigger_run(repo, args.yes)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='notebooklm-radio setup wizard', formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        'command',
        nargs='?',
        choices=['init', 'doctor', 'renew'],
        default='init',
        help='init (default): first-time setup; doctor: read-only checks; renew: credential renewal',
    )
    parser.add_argument('--repo', help='OWNER/NAME (default: the origin remote of this clone)')
    parser.add_argument('--profile', default='ci', help='notebooklm login profile used for CI (default: ci)')
    parser.add_argument(
        '--webhook', help='webhook URL to store as NOTIFY_WEBHOOK_URL (otherwise asked interactively, hidden)'
    )
    parser.add_argument('--yes', '-y', action='store_true', help='accept defaults without asking')
    parser.add_argument('--no-run', action='store_true', help='do not trigger the workflow at the end')
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
