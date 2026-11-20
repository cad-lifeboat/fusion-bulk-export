"""scripts/privacy_scan.py: clean repositories pass, a banned word in a file or a commit message fails."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('privacy_scan', ROOT / 'scripts' / 'privacy_scan.py')
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)

# Built from pieces, so that this file holds no banned word itself and passes its own scan.
BANNED = 'Mini' + '256'
SPACED = 'CAD' + ' Le' + 'ap'


@unittest.skipIf(shutil.which('git') is None, 'needs git')
class PrivacyScanTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name)
        self.git('init', '-q')

    def git(self, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.com', GIT_COMMITTER_NAME='t',
                   GIT_COMMITTER_EMAIL='t@example.com')
        subprocess.run(['git', '-C', str(self.repo), '-c', 'commit.gpgsign=false'] + list(args), check=True,
                       env=env, stdout=subprocess.DEVNULL)
        return subprocess.run(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL).stdout.decode().strip()

    def commit(self, name, text, message):
        (self.repo / name).write_bytes(text.encode('utf-8'))
        self.git('add', name)
        return self.git('commit', '-q', '-m', message)

    def run_scan(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = scan.main([str(self.repo)])
        return status, out.getvalue().splitlines()

    def test_the_pattern_finds_the_pieces_in_any_case(self):
        self.assertTrue(scan.re.search(scan.PATTERN, BANNED.lower()))
        self.assertTrue(scan.re.search(scan.PATTERN, SPACED, scan.re.IGNORECASE))
        self.assertFalse(scan.re.search(scan.PATTERN, 'Fusion Bulk Export', scan.re.IGNORECASE))

    def test_an_empty_repository_is_clean(self):
        self.assertEqual(self.run_scan()[0], 0)

    def test_a_clean_history_passes(self):
        # The last commit of the walk matches nothing: the shell loop this replaces then failed.
        for number in range(3):
            self.commit('f%d.txt' % number, 'plain text %d\n' % number, 'Plain change %d' % number)
        status, lines = self.run_scan()
        self.assertEqual(status, 0)
        self.assertEqual(lines, ['Privacy scan: no banned word in the tracked files or the commit messages.'])

    def test_a_file_with_a_banned_word_is_listed(self):
        self.commit('ok.txt', 'fine\n', 'First')
        self.commit('notes.md', 'Tested on the %s today\n' % BANNED, 'Second')
        status, lines = self.run_scan()
        self.assertEqual(status, 1)
        self.assertEqual(lines[0], 'notes.md')
        self.assertIn('The file listed above contains a banned word', lines[1])
        self.assertNotIn(BANNED.lower(), '\n'.join(lines).lower())  # the word itself is never printed

    def test_a_commit_message_with_a_banned_word_is_listed_by_id(self):
        first = self.commit('a.txt', 'a\n', 'Fix from the %s run' % SPACED.lower())
        self.commit('b.txt', 'b\n', 'Plain follow-up')
        status, lines = self.run_scan()
        self.assertEqual(status, 1)
        self.assertEqual(lines, [first, '::error::The commit listed above has a banned word in its message.'])

    def test_a_banned_word_in_the_body_of_a_message_counts(self):
        bad = self.commit('a.txt', 'a\n', 'Subject\n\nBody naming %s.' % BANNED)
        self.assertEqual(scan.bad_commits(self.repo), [bad])

    def test_git_failing_is_status_2(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = scan.main([str(self.repo / 'no-such-folder')])
        self.assertEqual(status, 2)
        self.assertIn('could not run', out.getvalue())

if __name__ == '__main__':
    unittest.main()
