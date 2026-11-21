""""Keep only each design's newest version" (off by default): once a design's newest version is in
the folder, complete and checked, the files this export recorded for its older versions are
removed, and only those, and only while they still match their record. Off, nothing is removed."""
import csv
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from tests import fake_adsk as fake
from fusion_bulk_export import checks, core, report
from fusion_bulk_export.core import Exporter

fake.install()
from fusion_bulk_export.fusion import FusionAdapter  # noqa: E402


class KeepLatestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.adapter = FusionAdapter(NS(documents=fake.Documents()))
        self.part = fake.File('Part', 'part')
        self.other = fake.File('Other', 'other')
        self.files = [self.part, self.other]

    def run_export(self, **kwargs):
        self.exporter = Exporter(self.root, self.adapter)
        self.counts = self.exporter.run(fake.data(self.files), **kwargs)
        self.manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        return self.counts

    def names(self):
        return sorted(path.name for path in self.root.rglob('*') if path.suffix in ('.f3d', '.step', '.png'))

    def entry(self, identity, version):
        return next(e for e in self.manifest['entries'] if e['id'] == identity and e['version'] == version)

    def versions(self, *numbers, **kwargs):
        """Export the part at each version number in turn (the other design stays at v3)."""
        for number in numbers:
            self.part.versionNumber = self.part.latestVersionNumber = number
            self.run_export(**kwargs)

    def test_off_nothing_is_ever_removed_across_a_multi_version_folder(self):
        removed = []
        real_unlink, real_remove = os.unlink, os.remove

        def unlink(path, *args, **kwargs):
            removed.append(str(path))
            return real_unlink(path, *args, **kwargs)

        def remove(path, *args, **kwargs):
            removed.append(str(path))
            return real_remove(path, *args, **kwargs)
        with patch('os.unlink', unlink), patch('os.remove', remove):
            self.versions(3, 4, 5)
            self.run_export()  # up to date
        recorded = {str(self.root / info['path']) for e in self.manifest['entries'] for info in core.recorded_files(e)}
        self.assertEqual(len(recorded), 12)  # three versions of the part and the other design, 3 files each
        self.assertFalse(recorded & set(removed))
        self.assertTrue(all(Path(path).is_file() for path in recorded))
        self.assertNotIn('removed', self.exporter.run_info)
        self.assertNotIn('keep_latest_only', self.exporter.run_info)
        self.assertFalse(any(e['status'] == 'removed' for e in self.manifest['entries']))

    def test_a_new_version_removes_the_older_versions_files_once_it_is_saved(self):
        self.versions(3)
        self.versions(4, keep_latest_only=True)
        self.assertEqual(self.names(), ['Other v3.f3d', 'Other v3.png', 'Other v3.step',
                                        'Part v4.f3d', 'Part v4.png', 'Part v4.step'])
        old = self.entry('part', 3)
        self.assertEqual(old['status'], 'removed')
        self.assertTrue(old['removed'])
        self.assertEqual(sorted(info['role'] for info in old['removed_files']), ['archive', 'step', 'thumbnail'])
        self.assertEqual((old['files'], old.get('thumbnail')), ([], None))
        self.assertEqual(self.entry('part', 4)['status'], 'done')
        self.assertEqual(self.exporter.run_info['removed'], dict(versions=1, files=3, kept=0, errors=0))
        self.assertIs(self.exporter.run_info['keep_latest_only'], True)
        # check: a removed version is counted, never a problem.
        totals = checks.check_folder(self.root)
        self.assertEqual((totals['failed_checks'], totals['removed'], totals['done']), (0, 1, 2))
        # The summary says how many.
        data = report.summary(self.counts, self.exporter.run_info, self.manifest, self.root)
        self.assertIn('Removed 1 older version (3 files).', data['notes'])
        # A later run has nothing more to remove, and says nothing of it.
        self.run_export(keep_latest_only=True)
        self.assertEqual(self.exporter.run_info['removed'], dict(versions=0, files=0, kept=0, errors=0))
        data = report.summary(self.counts, self.exporter.run_info, self.manifest, self.root)
        self.assertFalse([note for note in data['notes'] if 'older version' in note])

    def test_a_folder_already_up_to_date_is_cleaned_up_by_one_run_with_the_box_ticked(self):
        self.versions(3, 4, 5)  # three versions side by side, as before the option existed
        self.assertEqual(len(self.names()), 12)
        calls = self.part.calls
        self.run_export(keep_latest_only=True)
        self.assertEqual((self.counts['exported'], self.counts['up_to_date']), (0, 2))
        self.assertEqual(self.part.calls, calls)  # nothing was opened in Fusion again
        self.assertEqual(self.names(), ['Other v3.f3d', 'Other v3.png', 'Other v3.step',
                                        'Part v5.f3d', 'Part v5.png', 'Part v5.step'])
        self.assertEqual([self.entry('part', n)['status'] for n in (3, 4, 5)], ['removed', 'removed', 'done'])
        self.assertEqual(self.exporter.run_info['removed'], dict(versions=2, files=6, kept=0, errors=0))
        data = report.summary(self.counts, self.exporter.run_info, self.manifest, self.root)
        self.assertIn('Removed 2 older versions (6 files).', data['notes'])

    def test_an_older_file_changed_since_it_was_exported_keeps_that_whole_version(self):
        self.versions(3)
        step = self.root / self.entry('part', 3)['files'][1]['path']
        self.assertTrue(step.name.endswith('.step'))
        text = step.read_bytes()
        step.write_bytes(text[:-2] + b'X\n')  # same size, other content: only the SHA-256 tells
        self.assertEqual(len(step.read_bytes()), len(text))
        self.versions(4, keep_latest_only=True)
        self.assertIn('Part v3.step', self.names())
        self.assertIn('Part v3.f3d', self.names())  # nothing of that version is removed
        old = self.entry('part', 3)
        self.assertEqual((old['status'], old['kept']), ('done', core.KEPT_CHANGED))
        self.assertEqual(old['kept'], 'kept: changed since it was exported')
        self.assertEqual(self.exporter.run_info['removed'], dict(versions=0, files=0, kept=1, errors=0))
        data = report.summary(self.counts, self.exporter.run_info, self.manifest, self.root)
        self.assertIn('1 older version kept as it was (changed since it was exported); the report says which.',
                      data['notes'])
        page = report.render_html(self.manifest, self.exporter.run_info, self.counts)
        self.assertIn('Older version kept: changed since it was exported', page)

    def test_with_only_size_and_time_recorded_both_must_match(self):
        self.versions(3)
        manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        for entry in manifest['entries']:
            for info in core.recorded_files(entry):
                info.pop('sha256')
        (self.root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        archive = self.root / self.entry('part', 3)['files'][0]['path']
        os.utime(str(archive), (1600000000, 1600000000))  # same size, another time
        self.versions(4, keep_latest_only=True)
        self.assertEqual(self.entry('part', 3)['kept'], core.KEPT_CHANGED)
        self.assertTrue(archive.is_file())
        # Its time put back as recorded: size and time both match now, so it goes.
        recorded = self.entry('part', 3)['files'][0]['mtime_ns']
        os.utime(str(archive), ns=(recorded, recorded))
        self.run_export(keep_latest_only=True)
        self.assertEqual(self.entry('part', 3)['status'], 'removed')
        self.assertFalse(archive.exists())

    def test_nothing_is_removed_when_the_new_versions_step_failed(self):
        self.versions(3)
        original = fake.Manager.execute
        fake.Manager.execute = lambda manager, path: False if path.endswith('.step') else original(manager, path)
        try:
            self.versions(4, keep_latest_only=True)
        finally:
            fake.Manager.execute = original
        self.assertEqual(self.entry('part', 4)['step'], core.STEP_FAILED)
        self.assertIn('Part v3.step', self.names())
        self.assertIn('Part v3.f3d', self.names())
        self.assertEqual(self.entry('part', 3)['status'], 'done')
        # The next run writes v4's STEP; then v3 goes.
        self.run_export(keep_latest_only=True)
        self.assertEqual(self.entry('part', 3)['status'], 'removed')
        self.assertIn('Part v4.step', self.names())

    def test_nothing_is_removed_when_the_new_version_failed(self):
        self.versions(3)
        self.part.open_error = 'The design cannot be opened'
        self.versions(4, keep_latest_only=True)
        self.assertEqual(self.entry('part', 4)['status'], 'failed')
        self.assertEqual(self.entry('part', 3)['status'], 'done')
        self.assertIn('Part v3.f3d', self.names())

    def test_only_files_the_export_recorded_for_the_same_design_are_removed(self):
        self.versions(3)
        location = self.root / 'Hub' / 'Project'
        # The user's own file, named like an older version, and a file outside the export folder
        # that a damaged manifest points at.
        (location / 'Part v2.f3d').write_bytes(b'mine')
        outside = Path(self.temp.name).resolve().parent / ('outside-%s.f3d' % os.getpid())
        outside.write_bytes(b'not the export\'s')
        self.addCleanup(lambda: outside.exists() and outside.unlink())
        manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        stray = dict(manifest['entries'][0], key='["stray"]', version=1, thumbnail=None,
                     files=[dict(path='../' + outside.name, role='archive', bytes=len(b'not the export\'s'),
                                 sha256=checks.file_sha256(outside))])
        manifest['entries'].append(stray)
        (self.root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        self.versions(4, keep_latest_only=True)
        self.assertTrue((location / 'Part v2.f3d').is_file())
        self.assertTrue(outside.is_file())
        self.assertEqual(self.entry('part', 1)['kept'], core.KEPT_OUTSIDE)
        self.assertEqual(self.entry('part', 3)['status'], 'removed')
        # The other design (another id) keeps its files, though older than the part's v4.
        self.assertEqual([n for n in self.names() if n.startswith('Other')],
                         ['Other v3.f3d', 'Other v3.png', 'Other v3.step'])
        self.assertEqual(self.entry('other', 3)['status'], 'done')

    def test_a_file_another_entry_also_records_is_kept(self):
        self.versions(3)
        manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        part = next(e for e in manifest['entries'] if e['id'] == 'part')
        other = next(e for e in manifest['entries'] if e['id'] == 'other')
        other['files'].append(dict(part['files'][0]))  # a (damaged) manifest naming the file twice
        (self.root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        self.versions(4, keep_latest_only=True)
        self.assertEqual(self.entry('part', 3)['kept'], core.KEPT_SHARED)
        self.assertIn('Part v3.f3d', self.names())

    def test_a_file_that_cannot_be_deleted_is_recorded_and_tried_again(self):
        self.versions(3)
        real = os.unlink

        def refuse(path, *args, **kwargs):
            if str(path).endswith('Part v3.step'):
                raise PermissionError(13, 'Permission denied')
            return real(path, *args, **kwargs)
        with patch('os.unlink', refuse):
            self.versions(4, keep_latest_only=True)
        old = self.entry('part', 3)
        self.assertEqual(old['status'], 'removed')
        self.assertEqual([info['path'].rsplit('/', 1)[-1] for info in old['files']], ['Part v3.step'])
        self.assertIn('Permission denied', old['remove_error'])
        self.assertEqual(self.exporter.run_info['removed'], dict(versions=1, files=2, kept=0, errors=1))
        self.assertEqual(self.counts['exported'], 1)  # the run went on
        data = report.summary(self.counts, self.exporter.run_info, self.manifest, self.root)
        self.assertIn('1 older version could not be removed; the next run tries again (see the report).',
                      data['notes'])
        self.run_export(keep_latest_only=True)
        old = self.entry('part', 3)
        self.assertEqual((old['files'], old.get('remove_error')), ([], None))
        self.assertEqual(len(old['removed_files']), 3)
        self.assertNotIn('Part v3.step', self.names())

    def test_a_run_that_stopped_after_removing_finishes_the_record(self):
        """Files removed, but the run stopped before its record was saved: the next run marks the
        entry removed without counting files it did not remove."""
        self.versions(3)
        for info in core.recorded_files(self.entry('part', 3)):
            (self.root / info['path']).unlink()
        self.versions(4, keep_latest_only=True)
        old = self.entry('part', 3)
        self.assertEqual(old['status'], 'removed')
        self.assertEqual(self.exporter.run_info['removed']['files'], 0)

    def test_the_removal_is_in_the_journal_before_the_next_full_save(self):
        self.versions(3)
        with patch.object(core, 'SAVE_EVERY_ITEMS', 10 ** 6), patch.object(core, 'SAVE_EVERY_SECONDS', 10 ** 6):
            exporter = Exporter(self.root, self.adapter)
            self.part.versionNumber = 4
            with patch.object(Exporter, 'save', side_effect=lambda: None):
                exporter.run(fake.data(self.files), keep_latest_only=True)
        manifest = checks.read_manifest(self.root)
        old = next(e for e in manifest['entries'] if e['id'] == 'part' and e['version'] == 3)
        self.assertEqual(old['status'], 'removed')

    def test_report_lists_removed_versions_collapsed_and_csv_has_removed_rows(self):
        self.versions(3, 4)
        self.versions(5, keep_latest_only=True)
        page = report.render_html(self.manifest, self.exporter.run_info, self.counts)
        self.assertIn('<details><summary><h2 style="display:inline">Removed older versions (2 versions, 6 files)',
                      page)
        self.assertIn('Part v3 <span class="muted">', page)
        self.assertIn('Hub/Project/Part v4.step', page)
        self.assertIn('In this folder: 2 designs exported', page)  # removed ones are not designs here
        rows = list(csv.DictReader(io.StringIO(report.render_csv(self.manifest, self.exporter.run_info))))
        removed = [row for row in rows if row['status'] == 'removed']
        self.assertEqual(sorted(row['version'] for row in removed), ['3', '4'])
        self.assertTrue(all(row['note'].startswith('Removed: Hub/Project/Part v') for row in removed))
        self.assertTrue(all(row['design'] == 'Part' and row['archive'] == '' for row in removed))
        self.assertEqual(len([row for row in rows if row['status'] == 'done']), 2)
        self.assertEqual(report.failures(self.manifest), [])

    def test_removed_text_is_plural_correct(self):
        self.assertEqual(report.removed_text(1, 1), 'Removed 1 older version (1 file).')
        self.assertEqual(report.removed_text(2, 5), 'Removed 2 older versions (5 files).')

    def test_link_paths_never_point_at_a_removed_version(self):
        self.versions(3, 4)
        self.versions(5, keep_latest_only=True)
        manifest = dict(entries=[e for e in self.manifest['entries']] + [
            dict(kind='design', status='done', id='asm', files=[], references=[dict(id='part')])])
        core.link_paths(manifest)
        self.assertEqual(manifest['entries'][-1]['references'][0]['path'], 'Hub/Project/Part v5.f3d')


if __name__ == '__main__':
    unittest.main()
