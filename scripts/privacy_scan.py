#!/usr/bin/env python3
"""Check that no private name is in the tracked files or in a commit message.

Usage: privacy_scan.py [REPOSITORY]   (default: the current folder)

Lists the tracked text files and the commits (by id) whose message has a banned word, and
exits 1 when there is any; 0 when the repository is clean; 2 when git itself fails. It never
prints the words: they are stored encoded so that this file does not contain them itself.
Every commit reachable from HEAD is checked, with one `git log`; the CI job fetches the
whole history for it (actions/checkout with fetch-depth: 0).
"""
import base64
import re
import subprocess
import sys

ENCODED = ('ZHV0Y2h8c29saWRjcmFmdHxjYWRtcGVnfGNhZCA/bGVhcHxjbGF1ZGV8Y29kZXh8YW50aHJvcGljfG9wZW5haXxyYXlr'
           'aG9sb3xzcGFuaWVyfGNvaGVzaW9ufHJvdGFyeXxhZG9tfG5lbWExN3xtaW5pMjU2fHRhaWwyNjYz')
PATTERN = base64.b64decode(ENCODED).decode('ascii')


class GitError(Exception):
    pass


def git(repository, *args, allowed=(0,)):
    result = subprocess.run(['git', '-C', str(repository)] + list(args), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    if result.returncode not in allowed:
        raise GitError('git %s failed (%d): %s' % (args[0], result.returncode,
                                                   result.stderr.decode('utf-8', 'replace').strip()))
    return result


def bad_files(repository, pattern=PATTERN):
    """Tracked text files that contain the pattern (case-insensitive), as git names them."""
    # git grep: 0 = matches, 1 = none, anything else = an error.
    result = git(repository, '-c', 'core.quotePath=false', 'grep', '-IilE', pattern, allowed=(0, 1))
    return result.stdout.decode('utf-8', 'replace').splitlines()


def bad_commits(repository, pattern=PATTERN):
    """Ids of the commits reachable from HEAD whose message contains the pattern."""
    if git(repository, 'rev-parse', '--verify', '-q', 'HEAD', allowed=(0, 1)).returncode:
        return []  # no commits yet
    log = git(repository, 'log', '--format=%H%x00%B%x00').stdout.decode('utf-8', 'replace')
    fields = log.split('\0')
    words = re.compile(pattern, re.IGNORECASE)
    return [commit.strip() for commit, message in zip(fields[0::2], fields[1::2]) if words.search(message)]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    repository = argv[0] if argv else '.'
    try:
        files, commits = bad_files(repository), bad_commits(repository)
    except (GitError, OSError) as error:
        print('::error::The privacy scan could not run: %s' % error)
        return 2
    for name in files:
        print(name)
    if files:
        print('::error::The %d file(s) listed above contain a banned word. Remove it before merging.'
              % len(files))
    for commit in commits:
        print(commit)
    if commits:
        print('::error::The %d commit(s) listed above have a banned word in their message.' % len(commits))
    if not files and not commits:
        print('Privacy scan: no banned word in the tracked files or the commit messages.')
    return 1 if files or commits else 0


if __name__ == '__main__':
    sys.exit(main())
