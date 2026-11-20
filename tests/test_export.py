import errno
import os
import subprocess
import sys
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace as NS

from tests import fake_adsk as fake
from fusion_bulk_export import core, power
from fusion_bulk_export.core import Exporter, Selection, atomic_write, free_bytes, sanitize, walk

fake.install()
from fusion_bulk_export.fusion import FusionAdapter


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = NS(documents=fake.Documents())
        self.adapter = FusionAdapter(self.app)

    def run_export(self, data, **kwargs):
        exporter = Exporter(self.root, self.adapter)
        result = exporter.run(data, **kwargs)
        self.manifest = json.loads((self.root / 'manifest.json').read_text())
        return exporter, result

    def test_walk_every_hub_project_and_nested_folder(self):
        a, b = fake.File(), fake.File(identity='file-2')
        data = fake.data([a], [fake.folder('Nested', folders=[fake.folder('Deep', files=[b])])])
        other = fake.data([fake.File(identity='file-3')]).dataHubs.item(0)
        other.id = 'hub-2'
        other.dataProjects.items.append(NS(id='project-2', name='Other', rootFolder=fake.folder(files=[a])))
        data.dataHubs.items.append(other)
        items = list(walk(data))
        self.assertEqual(len(items), 4)
        self.assertEqual(items[1][2], ['Nested', 'Deep'])
        _, counts = self.run_export(data)
        self.assertEqual(counts['done'], 4)

    def selection_data(self):
        root, top, deep, other = (fake.File(identity=i) for i in ('root', 'top', 'deep', 'other'))
        sub = fake.folder('Sub', files=[deep])
        data = fake.data([root], [fake.folder('Top', files=[top], folders=[sub]), fake.folder('Other', files=[other])])
        second = fake.File(identity='second')
        data.dataHubs.item(0).dataProjects.items.append(NS(id='project-2', name='Two', rootFolder=fake.folder(files=[second])))
        return data, dict(root=root, top=top, deep=deep, other=other, second=second)

    def exported(self):
        return sorted(entry['id'] for entry in self.manifest['entries'] if entry['status'] == 'done')

    def test_selection_whole_project(self):
        data, _ = self.selection_data()
        self.run_export(data, selection=Selection({'project-2': None}))
        self.assertEqual(self.exported(), ['second'])

    def test_selection_folder_includes_subfolders_only(self):
        data, files = self.selection_data()
        exporter, _ = self.run_export(data, selection=Selection({'project-1': [['Top']]}))
        self.assertEqual(self.exported(), ['deep', 'top'])
        self.assertEqual(exporter.run_info['selection'], '0 whole project(s), 1 folder(s)')
        self.assertEqual(files['other'].calls + files['root'].calls + files['second'].calls, 0)

    def test_selection_nested_folder_and_unselected_folders_not_listed(self):
        data, _ = self.selection_data()
        project = data.dataHubs.item(0).dataProjects.item(0)
        other = project.rootFolder.dataFolders.item(1)
        other.dataFolders = None  # descending into it would fail and be logged as unreadable
        exporter, _ = self.run_export(data, selection=Selection({'project-1': [['Top', 'Sub']]}))
        self.assertEqual(self.exported(), ['deep'])
        self.assertEqual(exporter.run_info['discovery_errors'], [])

    def test_selection_files_only_entries(self):
        data, _ = self.selection_data()
        self.run_export(data, selection=Selection({'project-1': [[''], ['Top', '']]}))
        self.assertEqual(self.exported(), ['root', 'top'])

    def test_selection_from_config(self):
        selection = Selection.from_config(['project-2'], {'project-1': ['Top/Sub', 'Other'], 'project-2': ['X']})
        self.assertEqual(selection.projects, {'project-2': None, 'project-1': [['Top', 'Sub'], ['Other']]})
        self.assertTrue(Selection.from_config().everything)
        with self.assertRaises(ValueError):
            Selection.from_config(folders={'p': ['/']})

    def test_streams_exports_and_reports_where_it_is(self):
        data, files = self.selection_data()
        events = []

        def update(state):
            events.append((state['phase'], state['project'], state['projects'], tuple(state['path']),
                           state['design'], state['name'], files['root'].calls))
        self.run_export(data, progress=update)
        # Project 2 is listed only after project 1's first design has been exported.
        second = next(e for e in events if e[1] == 2 and e[0] == 'listing')
        self.assertEqual(second[6], 1)
        self.assertEqual(events[0][:4], ('listing', 1, 2, ()))
        self.assertIn(('design', 1, 2, ('Top', 'Sub'), 3, 'Part', 1), events)
        self.assertEqual(self.exported(), ['deep', 'other', 'root', 'second', 'top'])

    def test_cancel_from_progress_before_open(self):
        file = fake.File()
        state = {'cancel': False}
        def update(progress):
            if progress['phase'] == 'design':
                state['cancel'] = True
        exporter, counts = self.run_export(fake.data([file]),
                                          cancelled=lambda: state['cancel'], progress=update)
        self.assertTrue(exporter.run_info['cancelled'])
        self.assertEqual(counts['done'], 0)
        self.assertEqual(file.calls, 0)

    def test_core_imports_without_adsk(self):
        result = subprocess.run([sys.executable, '-c',
                                 "import fusion_bulk_export.core; import sys; assert 'adsk' not in sys.modules"],
                                capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parents[1] / 'FusionBulkExport')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_archive_references_step_and_manifest(self):
        a, b = fake.File(), fake.File('Assembly', 'assembly', references=True)
        _, counts = self.run_export(fake.data([a, b]))
        self.assertEqual(counts, dict(done=2, failed=0, skipped=0))
        for entry in self.manifest['entries']:
            self.assertEqual(entry['version'], 3)
            self.assertEqual(entry['date'], 1700000000)
            self.assertIsNone(entry['error'])
            # Fusion can only write .f3d; links are recorded instead of bundled.
            self.assertEqual([Path(f['path']).suffix for f in entry['files']], ['.f3d', '.step'])
            for info in entry['files']:
                self.assertEqual(len(info['sha256']), 64)
                self.assertEqual(info['bytes'], len(b'Synthetic CAD placeholder'))
        self.assertEqual(self.manifest['entries'][0]['references'], [])
        # Root has one solid (plus one surface body); a visible part holds one hidden solid.
        self.assertTrue(all(entry['solid_bodies'] == 2 for entry in self.manifest['entries']))
        self.assertTrue(all(entry['visible_solid_bodies'] == 1 for entry in self.manifest['entries']))
        self.assertEqual(self.manifest['entries'][1]['references'], [dict(id='linked-assembly', name='Linked part')])
        self.assertTrue(all(doc.closed for file in [a, b] for doc in file.documents))

    def test_reported_success_without_file_is_failure(self):
        file = fake.File()
        with patch.object(fake.Manager, 'execute', return_value=True):
            self.run_export(fake.data([file]))
        entry = self.manifest['entries'][0]
        self.assertEqual(entry['status'], 'failed')
        self.assertIn('wrote no file', entry['error'])

    def test_new_version_keeps_previous_pair(self):
        file = fake.File()
        data = fake.data([file])
        self.run_export(data)
        previous = [info['path'] for info in self.manifest['entries'][0]['files']]
        file.versionNumber = 4
        self.run_export(data)
        self.assertEqual(len(self.manifest['entries']), 2)
        self.assertTrue(all((self.root / path).exists() for path in previous))
        self.assertTrue(all(' v4.' in info['path'] for info in self.manifest['entries'][1]['files']))

    def test_unreadable_manifest_is_preserved(self):
        manifest = self.root / 'manifest.json'
        manifest.write_text('{incomplete', encoding='utf-8')
        with self.assertRaises(json.JSONDecodeError):
            Exporter(self.root, self.adapter)
        self.assertEqual(manifest.read_text(encoding='utf-8'), '{incomplete')

    def test_step_failure_does_not_mark_archive_done(self):
        file = fake.File()
        original = fake.Manager.execute
        def execute(manager, path):
            return False if path.endswith('.step') else original(manager, path)
        with patch.object(fake.Manager, 'execute', execute):
            self.run_export(fake.data([file]))
        entry = self.manifest['entries'][0]
        self.assertEqual(entry['status'], 'failed')
        self.assertEqual(entry['files'], [])
        self.assertIn('STEP', entry['error'])
        self.assertTrue(all(doc.closed for doc in file.documents))

    def test_resume_and_corrupt_or_missing_files(self):
        file = fake.File()
        data = fake.data([file])
        self.run_export(data)
        _, counts = self.run_export(data)
        self.assertEqual(counts['skipped'], 1)
        self.assertEqual(file.calls, 1)
        path = self.root / self.manifest['entries'][0]['files'][0]['path']
        path.write_bytes(b'changed')
        self.run_export(data)
        self.assertEqual(file.calls, 2)
        path.unlink()
        self.run_export(data)
        self.assertEqual(file.calls, 3)
        self.assertEqual(len(self.manifest['entries']), 1)
        self.assertEqual(len(self.manifest['runs']), 4)
        self.assertIn('verified', (self.root / 'export.log').read_text())

    def test_retry_then_success(self):
        file = fake.File()
        file.failures = 1
        self.run_export(fake.data([file]))
        self.assertEqual(self.manifest['entries'][0]['status'], 'done')
        self.assertEqual(file.calls, 2)
        self.assertTrue(all(d.closed for d in file.documents))

    def test_two_attempt_limit_and_retry_next_run(self):
        file = fake.File()
        file.failures = 2
        self.run_export(fake.data([file]))
        entry = self.manifest['entries'][0]
        self.assertEqual(entry['attempts'], 2)
        self.assertEqual(entry['status'], 'failed')
        self.assertIn('Synthetic', entry['error'])
        self.run_export(fake.data([file]))
        self.assertEqual(file.calls, 3)
        self.assertEqual(self.manifest['entries'][0]['status'], 'done')

    def test_cancel_between_items_and_resume(self):
        a, b = fake.File(), fake.File(identity='file-2')
        exporter, counts = self.run_export(fake.data([a, b]), cancelled=lambda: a.calls > 0)
        self.assertTrue(exporter.run_info['cancelled'])
        self.assertEqual(counts['done'], 1)
        self.assertEqual(b.calls, 0)
        self.run_export(fake.data([a, b]))
        self.assertEqual(b.calls, 1)

    def test_non_design_skipped(self):
        files = [fake.File(extension='f2d'), fake.File(identity='cam', design=False), fake.File(extension='txt')]
        _, counts = self.run_export(fake.data(files))
        self.assertEqual(counts['skipped'], 3)
        self.assertEqual(files[0].calls, 0)
        self.assertTrue(all(e['status'] == 'skipped' and e['error'] for e in self.manifest['entries']))

    def test_atomic_write_leaves_old_manifest_on_replace_failure(self):
        target = self.root / 'manifest.json'
        atomic_write(target, {'before': True})
        with patch('fusion_bulk_export.core.os.replace', side_effect=OSError('Synthetic replacement failure')):
            with self.assertRaises(OSError):
                atomic_write(target, {'after': True})
        self.assertEqual(json.loads(target.read_text()), {'before': True})
        atomic_write(target, {'after': True})
        self.assertFalse(target.with_suffix('.json.tmp').exists())

    def test_sanitizing_collisions_and_long_paths(self):
        self.assertEqual(sanitize('CON.txt'), '_CON.txt')
        self.assertEqual(sanitize('a/b:*? .'), 'a_b___')
        files = [fake.File('A/B', 'one'), fake.File('A:B', 'two'), fake.File('a_b', 'three')]
        folders = [fake.folder('x' * 100, files=files)]
        for _ in range(20):
            folders = [fake.folder('Deep', folders=folders)]
        self.run_export(fake.data(folders=folders))
        paths = [f['path'] for e in self.manifest['entries'] for f in e['files']]
        self.assertEqual(len(set(p.casefold() for p in paths)), 6)
        self.assertTrue(all(len(str(self.root / p)) < 240 for p in paths))
        self.assertTrue(all(' v3.' in p for p in paths))

    def test_open_unmodified_design_is_exported_and_left_open(self):
        file = fake.File()
        document = fake.Document(file)
        self.app.documents.items.append(document)
        self.run_export(fake.data([file]))
        self.assertFalse(document.closed)
        self.assertEqual(file.calls, 0)
        self.assertEqual(self.manifest['entries'][0]['status'], 'done')

    def test_linked_documents_opened_by_an_assembly_are_closed(self):
        user = fake.Document(fake.File('Mine', 'user-file'), self.app.documents)
        self.app.documents.items.append(user)
        assembly = fake.File('Assembly', 'assembly', references=2)
        self.run_export(fake.data([assembly]))
        linked = [assembly.childReferences.item(i) for i in range(2)]
        self.assertEqual([len(f.documents) for f in linked], [1, 1])
        self.assertTrue(all(doc.closed for f in [assembly] + linked for doc in f.documents))
        self.assertFalse(user.closed)
        self.assertEqual(self.app.documents.items, [user])
        self.assertEqual(self.manifest['entries'][0]['status'], 'done')

    def test_linked_document_the_user_had_open_stays_open(self):
        assembly = fake.File('Assembly', 'assembly', references=2)
        mine = fake.Document(assembly.childReferences.item(0), self.app.documents)
        self.app.documents.items.append(mine)
        self.run_export(fake.data([assembly]))
        self.assertFalse(mine.closed)
        self.assertEqual(self.app.documents.items, [mine])

    def test_linked_documents_closed_when_export_fails_and_edited_ones_left(self):
        assembly = fake.File('Assembly', 'assembly', references=2)
        original = fake.Documents.open

        def open_and_edit(documents, file, visible):
            first = file.calls == 0
            document = original(documents, file, visible)
            if first:
                documents.items[-1].isModified = True  # the second linked design shows edits
            return document
        with patch.object(fake.Manager, 'execute', return_value=False), \
                patch.object(fake.Documents, 'open', open_and_edit):
            self.run_export(fake.data([assembly]))
        entry = self.manifest['entries'][0]
        self.assertEqual(entry['status'], 'failed')
        self.assertEqual(assembly.calls, 2)  # two attempts
        self.assertEqual(len(self.app.documents.items), 1)
        self.assertTrue(self.app.documents.items[0].isModified)
        self.assertEqual(self.app.documents.items[0].dataFile.id, 'linked-assembly-2')

    def test_open_design_with_unsaved_changes_or_other_version_is_refused(self):
        for change, message in [(dict(isModified=True), 'unsaved changes'),
                                (dict(dataFile=NS(id='file-1', versionNumber=2)), 'Another version')]:
            with self.subTest(message=message):
                self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
                self.root = Path(self.temp.name)
                file = fake.File()
                document = fake.Document(file)
                for key, value in change.items():
                    setattr(document, key, value)
                self.app.documents.items[:] = [document]
                self.run_export(fake.data([file]))
                self.assertFalse(document.closed)
                self.assertEqual(file.calls, 0)
                entry = self.manifest['entries'][0]
                self.assertEqual(entry['status'], 'failed')
                self.assertIn(message, entry['error'])
                self.temp.cleanup()

    def test_export_false_and_close_on_failure(self):
        file = fake.File()
        with patch.object(fake.Manager, 'execute', return_value=False):
            self.run_export(fake.data([file]))
        self.assertEqual(self.manifest['entries'][0]['status'], 'failed')
        self.assertTrue(all(d.closed for d in file.documents))

    def failing_files(self, count):
        files = [fake.File(identity='file-%d' % n) for n in range(count)]
        for file in files:
            file.failures = 2
        return files

    def test_five_failures_in_a_row_stop_and_retry_next_run(self):
        files = self.failing_files(7)
        exporter, counts = self.run_export(fake.data(files))
        self.assertEqual(counts, dict(done=0, failed=5, skipped=0))
        self.assertEqual([f.calls for f in files], [2] * 5 + [0, 0])
        self.assertIn('5 designs in a row failed', exporter.run_info['stopped_reason'])
        self.assertEqual(exporter.run_info['stopped_code'], 'failing')
        self.assertFalse(exporter.run_info['cancelled'])
        self.assertEqual(self.manifest['runs'][-1]['stopped_code'], 'failing')
        for file in files:
            file.failures = 0  # back online
        exporter, counts = self.run_export(fake.data(files))
        self.assertEqual(counts['done'], 7)
        self.assertIsNone(exporter.run_info['stopped_reason'])

    def test_a_success_resets_the_failure_count(self):
        files = self.failing_files(4) + [fake.File(identity='good')] + self.failing_files(4)
        exporter, counts = self.run_export(fake.data(files))
        self.assertEqual(counts, dict(done=1, failed=8, skipped=0))
        self.assertIsNone(exporter.run_info['stopped_reason'])

    def test_on_trouble_retry_carries_on_and_stop_stops(self):
        files = self.failing_files(12)
        asked = []

        def on_trouble(reason, last_error):
            asked.append((reason, last_error))
            return 'retry' if len(asked) == 1 else 'stop'
        exporter = Exporter(self.root, self.adapter)
        counts = exporter.run(fake.data(files), on_trouble=on_trouble)
        self.assertEqual(len(asked), 2)
        self.assertIn('Synthetic export failure', asked[0][1])
        self.assertEqual(counts['failed'], 10)
        self.assertEqual(files[10].calls, 0)
        self.assertEqual(exporter.run_info['stopped_code'], 'failing')

    def test_disk_full_stops_at_once(self):
        a, b = fake.File(), fake.File(identity='file-2')

        def full(manager, path):
            raise OSError(errno.ENOSPC, 'No space left on device')
        with patch.object(fake.Manager, 'execute', full):
            exporter, counts = self.run_export(fake.data([a, b]))
        self.assertEqual(exporter.run_info['stopped_code'], 'disk_full')
        self.assertEqual(counts['failed'], 1)
        self.assertEqual((a.calls, b.calls), (1, 0))  # no second attempt, no next design
        self.assertEqual(self.manifest['entries'][0]['status'], 'failed')
        self.assertTrue(all(d.closed for d in a.documents))

    def test_fusion_error_with_little_free_space_counts_as_full(self):
        file = fake.File()
        file.failures = 2
        with patch.object(core, 'free_bytes', return_value=10 << 20):
            exporter, _ = self.run_export(fake.data([file, fake.File(identity='file-2')]))
        self.assertEqual(exporter.run_info['stopped_code'], 'disk_full')
        self.assertTrue(exporter.run_info['low_space'])
        self.assertEqual(file.calls, 1)

    def test_output_folder_gone_stops_at_once(self):
        a, b = fake.File(), fake.File(identity='file-2')
        root = self.root / 'out'

        def vanish(manager, path):
            import shutil
            shutil.rmtree(root)
            raise RuntimeError('Synthetic: could not write')
        exporter = Exporter(root, self.adapter)
        with patch.object(fake.Manager, 'execute', vanish):
            counts = exporter.run(fake.data([a, b]))
        self.assertEqual(exporter.run_info['stopped_code'], 'output_missing')
        self.assertEqual((a.calls, b.calls), (1, 0))
        self.assertEqual(counts['failed'], 1)

    @unittest.skipIf(os.name != 'posix' or (hasattr(os, 'geteuid') and os.geteuid() == 0),
                     'needs POSIX permissions as a normal user')
    def test_unwritable_output_folder_stops_at_once(self):
        a, b = fake.File(), fake.File(identity='file-2')
        root = self.root / 'out'

        def lock(manager, path):
            os.chmod(root, 0o500)
            raise RuntimeError('Synthetic: could not write')
        exporter = Exporter(root, self.adapter)
        self.addCleanup(os.chmod, root, 0o700)
        with patch.object(fake.Manager, 'execute', lock):
            exporter.run(fake.data([a, b]))
        self.assertEqual(exporter.run_info['stopped_code'], 'output_unwritable')
        self.assertEqual(b.calls, 0)

    def test_low_space_flag_and_free_bytes(self):
        self.assertGreater(free_bytes(self.root), 0)
        self.assertGreater(free_bytes(self.root / 'not' / 'yet'), 0)
        exporter, _ = self.run_export(fake.data([fake.File()]))
        self.assertIn('low_space', exporter.run_info)
        with patch.object(core, 'free_bytes', return_value=500 << 20):
            exporter, counts = self.run_export(fake.data([fake.File(identity='other')]))
        self.assertTrue(exporter.run_info['low_space'])
        self.assertEqual(counts['done'], 1)  # a warning, not a stop

    def test_keep_awake_is_a_no_op_under_the_test_api(self):
        self.assertFalse(power.inside_fusion())
        with patch.object(power.subprocess, 'Popen') as popen:
            with power.keep_awake(platform='darwin'):
                pass
        popen.assert_not_called()

    def test_keep_awake_on_macos_runs_caffeinate_and_ends_it(self):
        with patch.object(power.subprocess, 'Popen') as popen:
            with power.keep_awake(platform='darwin', active=True):
                self.assertEqual(popen.call_args[0][0][:3], ['caffeinate', '-i', '-w'])
        popen.return_value.terminate.assert_called_once_with()

    def test_keep_awake_fails_silently(self):
        with patch.object(power.subprocess, 'Popen', side_effect=OSError('no caffeinate')):
            with power.keep_awake(platform='darwin', active=True):
                pass
        with power.keep_awake(platform='win32', active=True):  # no ctypes.windll here
            pass
        with power.keep_awake(platform='linux', active=True):
            pass

    def test_data_discovery_error_persisted(self):
        exporter = Exporter(self.root, self.adapter)
        with self.assertRaises(AttributeError):
            exporter.run(NS())
        manifest = json.loads((self.root / 'manifest.json').read_text())
        self.assertIn('error', manifest['runs'][0])
        self.assertIsNotNone(manifest['runs'][0]['finished'])


if __name__ == '__main__':
    unittest.main()
