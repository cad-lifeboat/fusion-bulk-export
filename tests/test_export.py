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
from fusion_bulk_export import core, names, power
from fusion_bulk_export.core import Exporter, Selection, atomic_write, free_bytes, sanitize, walk
from fusion_bulk_export.names import clean, fusion_path, long_path

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

    def paths(self):
        return sorted(f['path'] for e in self.manifest['entries'] for f in e['files'])

    def test_real_names_with_unicode(self):
        name = 'Motorhalterung f\u00fcr Achse Z (rev B)'
        inner = fake.folder('\u00dcbersicht', files=[fake.File(name, 'motor')], identity='f-2')
        self.run_export(fake.data(folders=[fake.folder('Gr\u00f6\u00dfen', folders=[inner], identity='f-1')]))
        expected = 'Hub/Project/Gr\u00f6\u00dfen/\u00dcbersicht/' + name + ' v3'
        self.assertEqual(self.paths(), [expected + '.f3d', expected + '.step'])
        self.assertTrue((self.root / (expected + '.f3d')).is_file())
        entry = self.manifest['entries'][0]
        self.assertEqual(self.manifest['schema'], 2)
        self.assertEqual(entry['location'], 'Hub/Project/Gr\u00f6\u00dfen/\u00dcbersicht')
        self.assertEqual(entry['identities'], ['hub:hub-1', 'project:project-1', 'folder:f-1', 'folder:f-2'])
        self.assertEqual((entry['base'], entry['shortened']), (name, False))

    def test_same_names_in_one_folder_get_a_suffix_and_keep_it(self):
        files = [fake.File('Bracket', 'one'), fake.File('bracket', 'two'), fake.File('Brack:et', 'three'),
                 fake.File('Brack_et', 'four')]
        data = fake.data(files)
        self.run_export(data)
        first = self.paths()
        bases = {e['id']: e['base'] for e in self.manifest['entries']}
        self.assertEqual((bases['one'], bases['three']), ('Bracket', 'Brack_et'))  # first come keeps the name
        self.assertEqual(bases['two'], 'bracket~' + names.tag('file:two'))  # same name but for case
        self.assertEqual(bases['four'], 'Brack_et~' + names.tag('file:four'))  # same after cleaning
        self.assertEqual(len(set(p.casefold() for p in first)), 8)
        for file in files:
            file.versionNumber = 4
        self.run_export(data)  # a new run, names rebuilt from the manifest: same choices
        new = {e['id']: e['base'] for e in self.manifest['entries'] if e['version'] == 4}
        old = {e['id']: e['base'] for e in self.manifest['entries'] if e['version'] == 3}
        self.assertEqual(new, old)

    def test_same_named_sibling_folders_stay_apart_by_id(self):
        a, b = fake.File('Part', 'a'), fake.File('Part', 'b')
        data = fake.data(folders=[fake.folder('Parts', [a], identity='p1'), fake.folder('Parts', [b], identity='p2')])
        self.run_export(data)
        locations = {e['id']: e['location'] for e in self.manifest['entries']}
        self.assertEqual(locations['a'], 'Hub/Project/Parts')
        self.assertTrue(locations['b'].startswith('Hub/Project/Parts~'))
        self.assertEqual({e['base'] for e in self.manifest['entries']}, {'Part'})
        items = list(walk(data))
        self.assertEqual(items[1][2], ['Parts'])
        self.assertEqual(items[1][2].ids, ['p2'])

    def test_hub_named_like_a_file_of_the_output_folder(self):
        data = fake.data([fake.File()])
        data.dataHubs.item(0).name = 'Manifest.JSON'
        self.run_export(data)
        self.assertTrue(self.manifest['entries'][0]['location'].startswith('Manifest.JSON~'))

    def test_clean_names(self):
        self.assertEqual(clean('CON.txt'), '_CON.txt')
        self.assertEqual(clean('lpt1'), '_lpt1')
        self.assertEqual(clean('a/b:*? .'), 'a_b___')
        self.assertEqual(clean(' x. . '), 'x')
        self.assertEqual(clean('...'), '_')
        self.assertEqual(clean('Cafe\u0301'), 'Caf\u00e9')  # NFC
        self.assertEqual(clean('\u65e5\u672c\u8a9e\u306e\u90e8\u54c1'), '\u65e5\u672c\u8a9e\u306e\u90e8\u54c1')
        self.assertEqual(len(clean('x' * 300)), 100)
        self.assertLessEqual(len(clean('\u90e8' * 100).encode('utf-8')), 200)
        self.assertEqual(sanitize('a/b:*? .'), 'a_b___')

    def test_deep_long_paths_keep_full_names(self):
        folders = [fake.folder('x' * 100, files=[fake.File('y' * 100)])]
        for _ in range(3):
            folders = [fake.folder('z' * 100, folders=folders)]
        self.run_export(fake.data(folders=folders))
        entry = self.manifest['entries'][0]
        self.assertFalse(entry['shortened'])
        self.assertGreater(len(str(self.root / entry['files'][0]['path'])), 400)
        self.assertEqual(entry['status'], 'done')

    def test_path_too_long_for_the_system_uses_the_shortened_form(self):
        folders = [fake.folder('x' * 100, files=[fake.File('y' * 100)])]
        for _ in range(9):
            folders = [fake.folder('z' * 100, folders=folders)]
        exporter = Exporter(self.root, self.adapter)
        exporter.platform = 'darwin'  # 1,023 bytes at most
        exporter.run(fake.data(folders=folders))
        entry = json.loads((self.root / 'manifest.json').read_text())['entries'][0]
        self.assertEqual((entry['status'], entry['shortened']), ('done', True))
        self.assertIsNone(entry['location'])
        self.assertIn('folders~', entry['files'][0]['path'])
        self.assertTrue((self.root / entry['files'][0]['path']).is_file())

    def test_folder_that_cannot_be_created_uses_the_shortened_form(self):
        real = os.makedirs

        def refuse(path, exist_ok=False):
            if 'Refused' in str(path):
                raise OSError(errno.ENAMETOOLONG, 'File name too long')
            return real(path, exist_ok=exist_ok)
        with patch('fusion_bulk_export.core.os.makedirs', refuse):
            self.run_export(fake.data(folders=[fake.folder('Refused', files=[fake.File()])]))
        entry = self.manifest['entries'][0]
        self.assertEqual((entry['status'], entry['shortened']), ('done', True))

    def test_windows_long_path_retries_shortened_after_a_failure(self):
        file = fake.File('y' * 100)
        file.failures = 1
        folders = [fake.folder('x' * 100, files=[file]), ]
        folders = [fake.folder('z' * 100, folders=folders)]
        exporter = Exporter(self.root, self.adapter)
        exporter.platform = 'win32'
        exporter.run(fake.data(folders=folders))
        entry = json.loads((self.root / 'manifest.json').read_text())['entries'][0]
        self.assertEqual((entry['status'], entry['attempts'], entry['shortened']), ('done', 2, True))

    def test_long_path_prefix(self):
        self.assertEqual(long_path('C:\\out\\a', windows=True), '\\\\?\\C:\\out\\a')
        self.assertEqual(long_path('C:/out/a', windows=True), '\\\\?\\C:\\out\\a')
        self.assertEqual(long_path('\\\\server\\share\\a', windows=True), '\\\\?\\UNC\\server\\share\\a')
        self.assertEqual(long_path('\\\\?\\C:\\a', windows=True), '\\\\?\\C:\\a')
        self.assertEqual(long_path('relative\\a', windows=True), 'relative\\a')
        self.assertEqual(long_path('/tmp/a', windows=False), '/tmp/a')
        self.assertEqual(fusion_path('C:\\a', windows=True), '\\\\?\\C:\\a')
        with patch.object(names, 'FUSION_LONG_PATHS', False):
            self.assertEqual(fusion_path('C:\\a', windows=True), 'C:\\a')
            self.assertTrue(names.too_long('C:\\' + 'x' * 300, 'win32'))
        self.assertFalse(names.too_long('C:\\' + 'x' * 300, 'win32'))

    def test_schema_1_folder_resumes_without_re_exporting(self):
        old = fake.File('Old', 'old')
        root_dir = self.root / 'Hub~12345678' / 'Project~12345678'
        root_dir.mkdir(parents=True)
        files = []
        for suffix in ('.f3d', '.step'):
            path = root_dir / ('Old~' + 'a' * 24 + ' v3' + suffix)
            path.write_bytes(b'old copy')
            files.append(core.file_info(path, self.root))
        key = json.dumps(['hub-1', 'project-1', 'old', 3])
        entry = {'key': key, 'hub': 'Hub', 'project': 'Project', 'path': [], 'name': 'Old', 'id': 'old',
                 'version': 3, 'date': 1700000000, 'files': files, 'status': 'done', 'error': None, 'attempts': 1}
        (self.root / 'manifest.json').write_text(json.dumps(dict(schema=1, runs=[{}], entries=[entry])))
        new = fake.File('New', 'new')
        _, counts = self.run_export(fake.data([old, new]))
        self.assertEqual(counts, dict(done=1, failed=0, skipped=1))
        self.assertEqual(old.calls, 0)
        self.assertEqual(self.manifest['schema'], 2)
        self.assertEqual(self.manifest['entries'][0], entry)  # untouched, files where they were
        self.assertIn('Hub/Project/New v3.f3d', self.paths())
        with self.assertRaises(ValueError):
            (self.root / 'manifest.json').write_text(json.dumps(dict(schema=3, runs=[], entries=[])))
            Exporter(self.root, self.adapter)

    def test_output_folder_rules(self):
        deep = self.root / ('o' * 150)
        exporter = Exporter(deep, self.adapter)  # no 100-character cap any more
        self.assertEqual(exporter.root, deep.resolve())
        blocker = self.root / 'a-file'
        blocker.write_text('x')
        with self.assertRaises(ValueError) as caught:
            Exporter(blocker, self.adapter)
        self.assertIn('output folder', str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            Exporter(blocker / 'inside', self.adapter)
        self.assertIn('cannot be created', str(caught.exception))

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

    def test_crash_mid_run_recovers_finished_items_from_the_journal(self):
        files = [fake.File(identity='file-%d' % n) for n in range(6)]
        files[5].failures = 2
        exporter = Exporter(self.root, self.adapter)
        real_save = Exporter.save
        with patch.object(Exporter, 'save', lambda self: None if self.saves else real_save(self)):
            exporter.run(fake.data(files))  # only the start-of-run save reaches disk
        manifest = json.loads((self.root / 'manifest.json').read_text())
        self.assertEqual(manifest['entries'], [])
        journal = (self.root / 'manifest.journal').read_text().splitlines()
        self.assertEqual(len(journal), 6)
        with (self.root / 'manifest.journal').open('a') as stream:
            stream.write('{"entry": {"key": "cut sh')  # the line being written when it crashed
        exporter, counts = self.run_export(fake.data(files))
        self.assertEqual(exporter.run_info['journal_replayed'], 6)
        self.assertEqual(counts, dict(done=1, failed=0, skipped=5))
        self.assertEqual([f.calls for f in files], [1] * 5 + [3])
        self.assertEqual(len(self.manifest['entries']), 6)
        self.assertFalse((self.root / 'manifest.journal').exists())

    def test_journal_replay_replaces_entries_with_the_same_key(self):
        file = fake.File()
        file.failures = 2
        self.run_export(fake.data([file]))
        with (self.root / 'manifest.journal').open('w') as stream:
            stream.write(json.dumps(dict(entry=dict(self.manifest['entries'][0], status='done', note=1))) + '\n')
            stream.write(json.dumps(dict(entry=dict(self.manifest['entries'][0], status='done', note=2))) + '\n')
        exporter = Exporter(self.root, self.adapter)
        self.assertEqual([e['note'] for e in exporter.manifest['entries']], [2])

    def test_manifest_written_in_full_only_now_and_then(self):
        files = [fake.File(identity='file-%d' % n) for n in range(5000)]
        exporter = Exporter(self.root, self.adapter)
        with patch('fusion_bulk_export.journal.os.fsync'):  # speed only: 5,000 syncs are slow on some disks
            exporter.run(fake.data(files))
        self.assertEqual(exporter.run_info['counts']['done'], 5000)
        self.assertLess(exporter.saves, 150)
        self.assertEqual(exporter.saves, 1 + 5000 // 50 + 1)
        manifest = json.loads((self.root / 'manifest.json').read_text())
        self.assertEqual(len(manifest['entries']), 5000)
        self.assertFalse((self.root / 'manifest.journal').exists())

    def test_manifest_saved_after_thirty_seconds_and_not_on_verified_skips(self):
        files = [fake.File(identity='file-%d' % n) for n in range(3)]
        exporter = Exporter(self.root, self.adapter)
        ticks = iter([0, 0, 10, 31, 31, 40, 40, 50, 50])
        exporter.clock = lambda: next(ticks, 60)
        exporter.saved_at = 0
        exporter.run(fake.data(files))
        self.assertEqual(exporter.saves, 3)  # start, the item at 31 s, end
        exporter = Exporter(self.root, self.adapter)
        writes = []
        with patch.object(core, 'atomic_write', side_effect=lambda *a: writes.append(a)):
            counts = exporter.run(fake.data(files))
        self.assertEqual(counts['skipped'], 3)
        self.assertEqual(len(writes), 2)  # start and end only
        self.assertFalse((self.root / 'manifest.journal').exists())

    def test_data_discovery_error_persisted(self):
        exporter = Exporter(self.root, self.adapter)
        with self.assertRaises(AttributeError):
            exporter.run(NS())
        manifest = json.loads((self.root / 'manifest.json').read_text())
        self.assertIn('error', manifest['runs'][0])
        self.assertIsNotNone(manifest['runs'][0]['finished'])


if __name__ == '__main__':
    unittest.main()
