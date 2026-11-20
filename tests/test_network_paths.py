"""Output folders on a mapped network drive (X:) or a network path (\\\\server\\share): the
drive letter is kept everywhere, network paths get the right long-path form, Fusion is only
handed local short paths, and files arrive whole."""
import errno
import ntpath
import os
from pathlib import Path, PureWindowsPath
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from tests import fake_adsk as fake
from fusion_bulk_export import core, names, report, staging

fake.install()
from fusion_bulk_export.core import Exporter  # noqa: E402
from fusion_bulk_export.fusion import FusionAdapter  # noqa: E402

MAPPED = 'X:\\Backups\\Fusion export'
UNC = '\\\\fileserver\\shared\\Backups\\Fusion export'


class WindowsPathTests(unittest.TestCase):
    def test_a_mapped_drive_keeps_its_letter(self):
        self.assertEqual(names.absolute_path(MAPPED, ntpath), MAPPED)
        self.assertEqual(names.absolute_path('X:\\Backups\\old\\..\\Fusion export\\.', ntpath), MAPPED)
        self.assertEqual(names.absolute_path(UNC, ntpath), UNC)

    def test_long_path_forms(self):
        self.assertEqual(names.long_path(MAPPED, windows=True), '\\\\?\\' + MAPPED)
        self.assertEqual(names.long_path(UNC, windows=True), '\\\\?\\UNC\\fileserver\\shared\\Backups\\Fusion export')
        self.assertNotIn('?\\\\\\', names.long_path(UNC, windows=True))
        prefixed = '\\\\?\\UNC\\fileserver\\shared\\x'
        self.assertEqual(names.long_path(prefixed, windows=True), prefixed)
        self.assertEqual(names.long_path(PureWindowsPath(UNC) / 'Hub' / 'Part v3.f3d', windows=True),
                         '\\\\?\\UNC\\fileserver\\shared\\Backups\\Fusion export\\Hub\\Part v3.f3d')

    def test_network_paths_are_told_apart_from_drives(self):
        self.assertTrue(names.is_unc(UNC))
        self.assertTrue(names.is_unc('//fileserver/shared'))
        for path in (MAPPED, 'C:\\FusionExport', '\\\\?\\C:\\x', '\\\\.\\pipe\\x', '/Users/someone'):
            self.assertFalse(names.is_unc(path), path)

    def test_the_root_rule_counts_network_paths_like_any_other(self):
        self.assertIsNone(names.output_root_problem(PureWindowsPath(UNC), 'win32'))
        self.assertIsNone(names.output_root_problem(PureWindowsPath(MAPPED), 'win32'))
        long_unc = PureWindowsPath(UNC + '\\' + 'x' * 200)
        self.assertIn('at most 200', names.output_root_problem(long_unc, 'win32'))

    def test_a_network_path_is_staged_in_the_local_temporary_folder(self):
        temporary = tempfile.gettempdir()
        on_share = staging.Staging(UNC, temporary=temporary, windows=True)
        self.assertTrue(str(on_share.folder).startswith(temporary))
        mapped = staging.Staging(MAPPED, temporary=temporary, windows=True)
        self.assertEqual(str(mapped.folder), str(Path(MAPPED) / staging.NAME))
        # Two network folders do not share a staging folder.
        other = staging.Staging(UNC + '2', temporary=temporary, windows=True)
        self.assertNotEqual(on_share.folder, other.folder)

    def test_free_space_falls_back_to_the_plain_path_on_a_share(self):
        asked = []

        def usage(path):
            asked.append(path)
            if path.startswith('\\\\?\\'):
                raise OSError(errno.EINVAL, 'not supported on this share')
            return NS(free=123)
        with patch.object(core.sys, 'platform', 'win32'), patch.object(names.sys, 'platform', 'win32'), \
                patch.object(core.shutil, 'disk_usage', usage):
            self.assertEqual(core.free_bytes(UNC), 123)
        self.assertTrue(asked[0].startswith('\\\\?\\UNC\\'))


class StagedMoveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_a_file_from_another_disk_arrives_whole_under_its_name(self):
        # The file-system calls get names.long_path()'s spelling of each path: on Windows
        # "\\?\C:\..." , not the path itself. Here the same is done with another spelling of
        # the same file ("<folder>/../<folder>/<name>"), so a fake that compares whole paths would
        # miss the staged file here as it did on Windows.
        def respelled(path):
            path = Path(path)
            return str(path.parent / '..' / path.parent.name / path.name)
        for spelling in (None, respelled):
            with self.subTest(spelling=spelling and spelling.__name__):
                self.cross_disk_move(spelling)

    def cross_disk_move(self, spelling):
        place = staging.Staging(self.root / 'out', temporary=self.root / 'tmp')
        staged = place.path('.f3d')
        Path(staged).write_bytes(b'archive')
        final = self.root / 'final.f3d'
        real_replace = os.replace
        moves = []

        def replace(source, target):
            moves.append((Path(source).name, Path(target).name))
            if Path(source).name == Path(staged).name:  # by name: the path's spelling varies
                raise OSError(errno.EXDEV, 'Invalid cross-device link')
            return real_replace(source, target)
        with patch.object(staging.os, 'replace', replace), \
                patch.object(staging, 'long_path', spelling or staging.long_path):
            place.place(staged, final)
        self.assertEqual(final.read_bytes(), b'archive')
        self.assertFalse(Path(staged).exists())
        self.assertEqual(moves, [(Path(staged).name, 'final.f3d'), ('final.f3d.partial', 'final.f3d')])
        self.assertFalse((self.root / 'final.f3d.partial').exists())
        final.unlink()

    def test_a_failed_copy_leaves_no_partial_file(self):
        place = staging.Staging(self.root / 'out', temporary=self.root / 'tmp')
        staged = place.path('.step')
        Path(staged).write_bytes(b'step')
        final = self.root / 'final.step'
        with patch.object(staging.os, 'replace', side_effect=OSError(errno.EXDEV, 'cross-device')), \
                patch.object(staging.shutil, 'copyfile', side_effect=OSError(errno.ENOSPC, 'No space left')):
            with self.assertRaises(OSError):
                place.place(staged, final)
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.is_file()), [])


class LinkedRootTests(unittest.TestCase):
    """The same rule where it can run here: a folder reached through a link keeps the path it was
    chosen by (on Windows a mapped drive behaves the same way under resolve())."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        real = Path(self.temp.name) / 'real'
        real.mkdir()
        self.link = Path(self.temp.name) / 'mapped'
        try:
            os.symlink(str(real), str(self.link), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('no symbolic links here')

    def test_the_chosen_path_is_kept_in_the_manifest_report_and_summary(self):
        exporter = Exporter(self.link, FusionAdapter(NS(documents=fake.Documents())))
        self.assertEqual(exporter.root, self.link)
        counts = exporter.run(fake.data([fake.File()]))
        page, table = report.write(exporter, counts)
        self.assertEqual(page, self.link / 'index.html')
        data = report.summary(counts, exporter.run_info, exporter.manifest, str(self.link), page)
        self.assertTrue(data['report'].startswith(str(self.link)))
        self.assertTrue(data['output'].startswith(str(self.link)))
        self.assertIn('href="Hub/Project/Part%20v3.f3d"', page.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
