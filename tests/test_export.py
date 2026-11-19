import subprocess
import sys
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace as NS

from tests import fake_adsk as fake
from fusion_bulk_export.core import Exporter, Selection, atomic_write, sanitize, walk

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

    def test_cancel_from_progress_before_open(self):
        file = fake.File()
        state = {'cancel': False}
        def update(index, total, name):
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

    def test_data_discovery_error_persisted(self):
        exporter = Exporter(self.root, self.adapter)
        with self.assertRaises(AttributeError):
            exporter.run(NS())
        manifest = json.loads((self.root / 'manifest.json').read_text())
        self.assertIn('error', manifest['runs'][0])
        self.assertIsNotNone(manifest['runs'][0]['finished'])


if __name__ == '__main__':
    unittest.main()
