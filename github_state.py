"""Persist ONLY news_state.sqlite3 in a separate bot-state branch.

Used by GitHub Actions after checkout with fetch-depth: 0. No account keys,
webhooks, source files, or .env files are copied into the state branch.
"""

import argparse
import os
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path


STATE_FILE = 'news_state.sqlite3'
STATE_REF = 'refs/remotes/origin/bot-state'


def git(folder, *args, data=None, env=None):
    result = subprocess.run(['git', *args], cwd=folder, input=data,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if result.returncode:
        # Do not echo credentials, remote URLs or the process environment.
        raise RuntimeError(f'git {args[0]} failed (exit {result.returncode}). Check Actions write permissions and logs.')
    return result.stdout


def parent_commit(folder):
    git(folder, 'rev-parse', '--show-toplevel')
    check = subprocess.run(['git', 'show-ref', '--verify', '--quiet', STATE_REF], cwd=folder,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check.returncode == 1:
        return None
    if check.returncode:
        raise RuntimeError('Cannot inspect state branch. Restore aborted; no history reset.')
    return git(folder, 'rev-parse', STATE_REF).decode().strip()


def validate_database(path):
    with closing(sqlite3.connect(path)) as db:
        db.execute('PRAGMA query_only=ON')
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('State database is damaged; refusing to reset delivery history.')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'seen', 'cached', 'pending', 'meta'}.issubset(tables):
            raise ValueError('State database has an unexpected schema.')


def restore(folder):
    folder = Path(folder).resolve()
    parent = parent_commit(folder)
    destination = folder / STATE_FILE
    if destination.exists():
        raise RuntimeError('Local state already exists; refusing to overwrite it. Use a fresh Actions checkout.')
    if parent is None:
        print('No bot-state branch yet. First run will notify only the latest article.')
        return
    data = git(folder, 'show', f'{parent}:{STATE_FILE}')
    fd, name = tempfile.mkstemp(prefix='.state-restore-', suffix='.tmp', dir=folder)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        validate_database(temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    print('Delivery history restored from bot-state.')


def save(folder):
    folder = Path(folder).resolve()
    path = folder / STATE_FILE
    if not path.is_file():
        print('No state file to save. No branch was changed.')
        return
    validate_database(path)
    parent = parent_commit(folder)
    blob = git(folder, 'hash-object', '-w', '--stdin', data=path.read_bytes()).decode().strip()
    with tempfile.TemporaryDirectory(prefix='apex-state-index-') as temporary:
        # A separate index guarantees only the database goes into the new tree.
        # The checked-out branch, source files and normal index stay untouched.
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(temporary) / 'index'),
                   GIT_AUTHOR_NAME='github-actions[bot]',
                   GIT_AUTHOR_EMAIL='41898282+github-actions[bot]@users.noreply.github.com',
                   GIT_COMMITTER_NAME='github-actions[bot]',
                   GIT_COMMITTER_EMAIL='41898282+github-actions[bot]@users.noreply.github.com')
        git(folder, 'read-tree', '--empty', env=env)
        git(folder, 'update-index', '--add', '--cacheinfo', f'100644,{blob},{STATE_FILE}', env=env)
        tree = git(folder, 'write-tree', env=env).decode().strip()
        if parent and git(folder, 'rev-parse', parent + '^{tree}').decode().strip() == tree:
            print('Delivery history is unchanged; no commit needed.')
            return
        args = ['commit-tree', tree]
        if parent:
            args += ['-p', parent]
        commit = git(folder, *args, '-m', 'Update Apex news delivery history', env=env).decode().strip()
    # No force push: concurrent external updates must fail visibly, not be lost.
    git(folder, 'push', 'origin', f'{commit}:refs/heads/bot-state')
    git(folder, 'update-ref', STATE_REF, commit)
    print('Delivery history saved to bot-state.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['restore', 'save'])
    args = parser.parse_args()
    try:
        (restore if args.command == 'restore' else save)(Path(__file__).resolve().parent)
    except (RuntimeError, ValueError, sqlite3.DatabaseError, OSError) as error:
        print('State sync failed:', error)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
