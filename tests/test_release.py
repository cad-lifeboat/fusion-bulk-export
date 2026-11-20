"""scripts/release.py prepare: bump, changelog, tests, zip, printed (not run) publish commands.
Runs against a temporary copy of the repository files it changes."""
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release', ROOT / 'scripts' / 'release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

MANIFEST = '''{
  "autodeskProduct": "Fusion360",
  "type": "addin",
  "version": "0.2.3",
  "runOnStartup": false
}
'''
CHANGELOG = '''# Changelog

## Unreleased

- New thing.
- Fixed thing.

## 0.2.3

- Older thing.
'''


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'repo'
        shutil.copytree(str(ROOT / 'FusionBulkExport'), str(self.root / 'FusionBulkExport'),
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copytree(str(ROOT / 'packaging'), str(self.root / 'packaging'))
        shutil.copy(str(ROOT / 'LICENSE'), str(self.root / 'LICENSE'))
        self.manifest = self.root / 'FusionBulkExport' / 'FusionBulkExport.manifest'
        self.manifest.write_text(MANIFEST, encoding='utf-8')
        self.changelog = self.root / 'CHANGELOG.md'
        self.changelog.write_text(CHANGELOG, encoding='utf-8')
        self.lines = []
        self.tested = []

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, version, passes=True):
        def tests(root):
            self.tested.append(root)
            return passes
        return release.prepare(version, self.root, tests=tests, echo=self.lines.append)

    def test_prepare_bumps_moves_changelog_tests_and_builds(self):
        target = self.prepare('0.3.0')
        self.assertEqual(self.tested, [self.root.resolve()])
        self.assertEqual(self.manifest.read_text(encoding='utf-8'), MANIFEST.replace('0.2.3', '0.3.0', 1))
        self.assertEqual(self.changelog.read_text(encoding='utf-8'),
                         CHANGELOG.replace('## Unreleased', '## 0.3.0'))
        self.assertEqual(target, self.root.resolve() / 'dist' / 'FusionBulkExport-0.3.0.zip')
        with zipfile.ZipFile(str(target)) as archive:
            self.assertIn('FusionBulkExport-0.3.0/README.txt', archive.namelist())
        notes = (self.root / 'dist' / 'release-notes-0.3.0.md').read_text(encoding='utf-8')
        self.assertEqual(notes, '- New thing.\n- Fixed thing.\n')

    def test_prints_publish_commands_without_running_them(self):
        self.prepare('0.3.0')
        printed = '\n'.join(self.lines)
        self.assertIn('git tag -a v0.3.0', printed)
        self.assertIn('git push origin HEAD v0.3.0', printed)
        self.assertIn('gh release create v0.3.0 "dist/FusionBulkExport-0.3.0.zip"', printed)
        self.assertIn('--notes-file "dist/release-notes-0.3.0.md"', printed)
        self.assertFalse((self.root / '.git').exists())

    def test_failed_tests_restore_both_files_and_build_nothing(self):
        with self.assertRaises(release.ReleaseError):
            self.prepare('0.3.0', passes=False)
        self.assertEqual(self.manifest.read_text(encoding='utf-8'), MANIFEST)
        self.assertEqual(self.changelog.read_text(encoding='utf-8'), CHANGELOG)
        self.assertFalse((self.root / 'dist').exists())

    def test_refuses_bad_old_or_duplicate_versions_and_empty_changelog(self):
        for version in ('0.3', 'v0.3.0', '0.2.3', '0.1.9'):
            with self.assertRaises(release.ReleaseError, msg=version):
                self.prepare(version)
        self.changelog.write_text(CHANGELOG.replace('- New thing.\n- Fixed thing.\n', ''), encoding='utf-8')
        with self.assertRaises(release.ReleaseError):
            self.prepare('0.3.0')
        self.changelog.write_text('# Changelog\n\n## 0.2.3\n\n- Older thing.\n', encoding='utf-8')
        with self.assertRaises(release.ReleaseError):
            self.prepare('0.3.0')
        self.assertEqual(self.manifest.read_text(encoding='utf-8'), MANIFEST)
        self.assertEqual(self.tested, [])

    def test_unreleased_as_the_last_section(self):
        text, notes = release.move_unreleased('# Changelog\n\n## Unreleased\n\n- Only thing.\n', '1.0.0')
        self.assertEqual(text, '# Changelog\n\n## 1.0.0\n\n- Only thing.\n')
        self.assertEqual(notes, '- Only thing.')

    def test_main_without_a_command_prints_help(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(release.main([]), 2)


if __name__ == '__main__':
    unittest.main()
