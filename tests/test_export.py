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
        self.assertEqual(counts['exported'], 4)

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

    def test_selection_by_folder_id_tells_same_named_siblings_apart(self):
        a, b, c = fake.File('A', 'a'), fake.File('B', 'b'), fake.File('C', 'c')
        inner = fake.folder('Inner', [c], identity='i1')
        data = fake.data(folders=[fake.folder('Twin', [a], identity='t1'),
                                  fake.folder('Twin', [b], [inner], identity='t2')])
        self.run_export(data, selection=Selection({'project-1': [[{'id': 't2', 'name': 'Twin'}]]}))
        self.assertEqual(self.exported(), ['b', 'c'])
        self.assertEqual(a.calls, 0)
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Own files only, mixed with a name below an id; by name alone both twins match.
        self.run_export(data, selection=Selection({'project-1': [[{'id': 't2'}, '']]}))
        self.assertEqual(self.exported(), ['b'])
        self.run_export(data, selection=Selection({'project-1': [[{'id': 't2'}, 'Inner']]}))
        self.assertEqual(self.exported(), ['b', 'c'])
        self.run_export(data, selection=Selection({'project-1': [['Twin']]}))
        self.assertEqual(self.exported(), ['a', 'b', 'c'])

    def test_selection_by_id_does_not_list_other_folders(self):
        other = fake.folder('Twin', [fake.File('A', 'a')], identity='t1')
        other.dataFolders = None  # listing it would fail and be logged
        data = fake.data(folders=[other, fake.folder('Twin', [fake.File('B', 'b')], identity='t2')])
        exporter, _ = self.run_export(data, selection=Selection({'project-1': [[{'id': 't2'}]]}))
        self.assertEqual(exporter.run_info['discovery_errors'], [])
        self.assertEqual(self.exported(), ['b'])

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
        self.assertEqual(counts['exported'], 0)
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
        self.assertEqual(counts, dict(exported=2, up_to_date=0, files=0, files_up_to_date=0, skipped=0, failed=0))
        for entry in self.manifest['entries']:
            self.assertEqual(entry['version'], 3)
            self.assertEqual(entry['date'], 1700000000)
            self.assertIsNone(entry['error'])
            # Fusion can only write .f3d; links are recorded instead of bundled.
            self.assertEqual([Path(f['path']).suffix for f in entry['files']], ['.f3d', '.step'])
            for info in entry['files']:
                self.assertEqual(len(info['sha256']), 64)
                self.assertEqual(info['bytes'], (self.root / info['path']).stat().st_size)
        self.assertEqual(self.manifest['entries'][0]['references'], [])
        # Root has one solid (plus one surface body); a visible part holds one hidden solid.
        self.assertTrue(all(entry['solid_bodies'] == 2 for entry in self.manifest['entries']))
        self.assertTrue(all(entry['visible_solid_bodies'] == 1 for entry in self.manifest['entries']))
        self.assertEqual(self.manifest['entries'][1]['references'], [dict(id='linked-assembly', name='Linked part')])
        self.assertTrue(all(doc.closed for file in [a, b] for doc in file.documents))

    def test_each_step_is_checked_inline_and_warnings_stored(self):
        good, empty, fewer = fake.File('Good', 'good'), fake.File('Empty', 'empty'), fake.File('Fewer', 'fewer')
        empty.step_solids = 0
        fewer.step_solids = 3
        _, counts = self.run_export(fake.data([good, empty, fewer]))
        self.assertEqual(counts['exported'], 3)  # warnings are not failures
        entries = {e['id']: e for e in self.manifest['entries']}
        self.assertEqual((entries['good']['step_solids'], entries['good']['warnings']), (1, []))
        self.assertEqual(entries['empty']['warnings'], ['STEP has no bodies (Fusion showed 1)'])
        self.assertEqual(entries['fewer']['warnings'], ['STEP has 3 solids, Fusion showed 1'])
        self.assertEqual(entries['fewer']['step_solids'], 3)

    def test_truncated_step_or_broken_archive_fails_and_is_retried(self):
        for broken in ('step', 'archive'):
            with self.subTest(broken=broken):
                self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
                self.addCleanup(self.temp.cleanup)
                self.root = Path(self.temp.name)
                file = fake.File()
                original = fake.Manager.execute

                def execute(manager, path):
                    original(manager, path)
                    if path.endswith('.step') and broken == 'step':
                        Path(path).write_bytes(fake.step_text(1, complete=False))
                    if path.endswith('.f3d') and broken == 'archive':
                        Path(path).write_bytes(b'not a zip')
                    return True
                with patch.object(fake.Manager, 'execute', execute):
                    self.run_export(fake.data([file]))
                entry = self.manifest['entries'][0]
                self.assertEqual((entry['status'], entry['attempts']), ('failed', 2))
                self.assertIn('incomplete' if broken == 'step' else 'damaged', entry['error'])
                self.run_export(fake.data([file]))  # a good export next time
                self.assertEqual(self.manifest['entries'][0]['status'], 'done')

    def test_visible_solids_count_each_shown_occurrence(self):
        part = fake.component('part', [fake.body(), fake.body(shown=False)])
        sub = fake.component('sub', [fake.body()])
        hidden_parent = fake.occurrence(sub, bulb=False, visible=False)
        occurrences = [fake.occurrence(part), fake.occurrence(part),             # two instances: two solids
                       fake.occurrence(part, visible=False),                      # hidden: none
                       hidden_parent, fake.occurrence(part, parent=hidden_parent),  # inside a hidden one
                       fake.occurrence(sub), fake.occurrence(part, parent=fake.occurrence(sub, bulb=False))]
        file = fake.File('Assembly', 'assembly')
        file.root = fake.component('root', [fake.body(), fake.body(solid=False)], occurrences)
        file.step_solids = 4
        self.run_export(fake.data([file]))
        entry = self.manifest['entries'][0]
        # root 1 + part twice + sub once = 4; each shown component once: root 1 + part 1 + sub 1 = 3
        self.assertEqual((entry['visible_solid_bodies'], entry['visible_solid_bodies_distinct']), (4, 3))
        self.assertEqual(entry['warnings'], [])
        file.step_solids = 3  # a STEP writer that writes a repeated part once: also a match
        file.versionNumber = 4
        self.run_export(fake.data([file]))
        self.assertEqual(self.manifest['entries'][-1]['warnings'], [])

    def test_counting_problems_never_stop_an_export(self):
        file = fake.File()
        file.root = fake.component('root', [fake.body()], [NS(component=fake.component('x'))])  # no isVisible
        _, counts = self.run_export(fake.data([file]))
        entry = self.manifest['entries'][0]
        self.assertEqual(counts['exported'], 1)
        self.assertIn('count_error', entry)
        self.assertEqual(entry['warnings'], [])  # nothing to compare with

    def test_step_solids_are_found_across_chunk_boundaries(self):
        from fusion_bulk_export import checks
        path = self.root / 'a.step'
        path.write_bytes(fake.step_text(40))
        for size in (7, 31, 64, 1 << 20):
            with patch.object(checks, 'CHUNK', size), patch.object(checks, 'OVERLAP', 64):
                self.assertEqual(checks.step_facts(path), ([], [], 40))

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
        self.assertEqual(counts["up_to_date"], 1)
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

    def test_resume_checks_size_and_time_without_hashing(self):
        from fusion_bulk_export import checks
        file = fake.File()
        data = fake.data([file])
        self.run_export(data)
        records = self.manifest['entries'][0]['files']
        self.assertTrue(all(isinstance(info['mtime_ns'], int) for info in records))
        real = checks.file_sha256
        hashed = []

        def counting(path):
            hashed.append(path)
            return real(path)
        with patch.object(checks, 'file_sha256', counting):
            _, counts = self.run_export(data)
            self.assertEqual((counts['up_to_date'], hashed), (1, []))
            # Touched but unchanged (copied without times, say): hashed once, time recorded.
            path = self.root / records[1]['path']
            os.utime(path, ns=(records[1]['mtime_ns'] + 10 ** 9,) * 2)
            _, counts = self.run_export(data)
            self.assertEqual((counts['up_to_date'], len(hashed), file.calls), (1, 1, 1))
            self.assertEqual(self.manifest['entries'][0]['files'][1]['mtime_ns'], path.stat().st_mtime_ns)
            self.run_export(data)
            self.assertEqual(len(hashed), 1)
            # Same size, other bytes, new time: the hash catches it.
            path.write_bytes(path.read_bytes()[::-1])
            self.run_export(data)
            self.assertEqual(file.calls, 2)

    def test_entries_without_times_are_hashed_once_then_get_them(self):
        from fusion_bulk_export import checks
        file = fake.File()
        data = fake.data([file])
        self.run_export(data)
        manifest = json.loads((self.root / 'manifest.json').read_text())
        for info in manifest['entries'][0]['files']:
            del info['mtime_ns']  # as written by earlier versions
        (self.root / 'manifest.json').write_text(json.dumps(manifest))
        hashed = []
        real = checks.file_sha256
        with patch.object(checks, 'file_sha256', lambda path: hashed.append(path) or real(path)):
            _, counts = self.run_export(data)
            self.assertEqual((counts['up_to_date'], len(hashed), file.calls), (1, 2, 1))
            self.assertTrue(all('mtime_ns' in info for info in self.manifest['entries'][0]['files']))
            self.run_export(data)
            self.assertEqual(len(hashed), 2)

    def test_size_change_is_caught_without_hashing(self):
        file = fake.File()
        data = fake.data([file])
        self.run_export(data)
        path = self.root / self.manifest['entries'][0]['files'][0]['path']
        stat = path.stat()
        with path.open('ab') as stream:
            stream.write(b'x')
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # even with the old time
        self.run_export(data)
        self.assertEqual(file.calls, 2)

    def test_files_get_the_design_date_and_resume_stays_fast(self):
        from fusion_bulk_export import checks
        file = fake.File()
        data = fake.data([file])
        self.run_export(data)
        entry = self.manifest['entries'][0]
        for info in entry['files'] + [entry['thumbnail']]:
            path = self.root / info['path']
            self.assertEqual(path.stat().st_mtime, 1700000000)
            self.assertEqual(info['mtime_ns'], path.stat().st_mtime_ns)
        with patch.object(checks, 'file_sha256', side_effect=AssertionError('hashed')):
            _, counts = self.run_export(data)
        self.assertEqual(counts['up_to_date'], 1)

    def test_bad_design_dates_leave_the_time_of_writing(self):
        from fusion_bulk_export.core import design_time
        now = 1800000000
        for bad in (None, 'yesterday', 0, -5, float('nan'), float('inf'), now + 10 ** 6, True, [1]):
            self.assertIsNone(design_time(bad, now), bad)
        self.assertEqual(design_time(1700000000, now), 1700000000)
        self.assertEqual(design_time('1700000000', now), 1700000000)
        self.assertEqual(design_time(1700000000123, now), 1700000000.123)  # milliseconds
        file = fake.File()
        file.dateModified = 'not a date'
        self.run_export(fake.data([file]))
        entry = self.manifest['entries'][0]
        self.assertEqual(entry['status'], 'done')
        self.assertGreater((self.root / entry['files'][0]['path']).stat().st_mtime, 1750000000)
        _, counts = self.run_export(fake.data([file]))
        self.assertEqual(counts['up_to_date'], 1)

    def test_thumbnail_saved_beside_the_files(self):
        self.run_export(fake.data([fake.File('Gear', 'gear')]))
        entry = self.manifest['entries'][0]
        self.assertEqual(entry['thumbnail']['path'], 'Hub/Project/Gear v3.png')
        self.assertEqual((self.root / 'Hub/Project/Gear v3.png').read_bytes(), fake.PNG)
        self.assertEqual(len(entry['files']), 2)  # the thumbnail is not one of the design's files

    def test_thumbnail_not_ready_or_failing_is_skipped_silently(self):
        def broken_save(path):
            Path(path).write_bytes(b'half')
            raise RuntimeError('Synthetic thumbnail failure')

        class Raising(fake.File):
            @property
            def thumbnail(self):
                raise RuntimeError('Synthetic: no thumbnail')

            @thumbnail.setter
            def thumbnail(self, value):
                pass
        cases = [('processing', fake.thumbnail('processing')), ('failed', fake.thumbnail('failed')),
                 ('none', None), ('empty', fake.thumbnail(data=b'')),
                 ('raising save', NS(state=1, dataObject=NS(saveToFile=broken_save))),
                 ('false save', NS(state=1, dataObject=NS(saveToFile=lambda path: False)))]
        files = []
        for name, future in cases:
            file = fake.File(name, name)
            file.thumbnail = future
            files.append(file)
        files.append(Raising('raising', 'raising'))
        _, counts = self.run_export(fake.data(files))
        self.assertEqual(counts['exported'], len(files))
        self.assertTrue(all('thumbnail' not in e for e in self.manifest['entries']))
        self.assertEqual(list((self.root / 'Hub' / 'Project').glob('*.png')), [])

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
        self.assertEqual(counts['exported'], 1)
        self.assertEqual(b.calls, 0)
        self.run_export(fake.data([a, b]))
        self.assertEqual(b.calls, 1)

    def test_non_design_skipped(self):
        files = [fake.File(identity='drawing', extension='f2d'), fake.File(identity='cam', design=False),
                 fake.File(identity='text', extension='txt')]
        _, counts = self.run_export(fake.data(files), other_files=False)
        self.assertEqual(counts['skipped'], 3)
        self.assertEqual((files[0].calls, files[0].downloads), (0, []))
        self.assertEqual([e['kind'] for e in self.manifest['entries']], ['file', 'design', 'file'])
        self.assertTrue(all(e['status'] == 'skipped' and e['error'] for e in self.manifest['entries']))

    def test_other_files_are_saved_under_their_own_names(self):
        # As on real Fusion, an uploaded file's name already holds its extension.
        spec, manual = fake.File('Spec.pdf', 'spec', extension='pdf'), fake.File('Manual.PDF', 'manual', extension='pdf')
        picture = fake.File('Photo.jpg', 'photo', extension='jpg')
        script = fake.File('notes.scr', 'script', extension='htm')  # fileExtension can disagree: trust the name
        design = fake.File('Part', 'part')
        data = fake.data([design], [fake.folder('Docs', [spec, manual, picture, script], identity='docs')])
        _, counts = self.run_export(data)
        self.assertEqual(counts, dict(exported=1, up_to_date=0, files=4, files_up_to_date=0, skipped=0, failed=0))
        entries = {e['id']: e for e in self.manifest['entries']}
        self.assertEqual([f['path'] for f in entries['spec']['files']], ['Hub/Project/Docs/Spec.pdf'])
        self.assertEqual(entries['manual']['files'][0]['path'], 'Hub/Project/Docs/Manual.PDF')
        self.assertEqual(entries['photo']['files'][0]['path'], 'Hub/Project/Docs/Photo.jpg')
        self.assertEqual(entries['script']['files'][0]['path'], 'Hub/Project/Docs/notes.scr')
        self.assertEqual((entries['spec']['kind'], entries['part']['kind']), ('file', 'design'))
        self.assertEqual((entries['spec']['base'], entries['spec']['extension']), ('Spec', 'pdf'))
        path = self.root / 'Hub/Project/Docs/Spec.pdf'
        self.assertEqual(path.read_bytes(), spec.content)
        self.assertEqual(path.stat().st_mtime, 1700000000)  # the file's date in Fusion
        self.assertEqual(entries['spec']['files'][0]['bytes'], len(spec.content))
        # Fusion is given a short file path in a fresh staging folder, which is gone afterwards.
        self.assertEqual(Path(spec.downloads[0]).parent.parent, self.root / '.staging')
        self.assertLess(len(spec.downloads[0]), 260)
        self.assertFalse((self.root / '.staging').exists())
        self.assertEqual(sorted(p.name for p in (self.root / 'Hub/Project/Docs').iterdir()),
                         ['Manual.PDF', 'Photo.jpg', 'Spec.pdf', 'notes.scr'])
        self.assertEqual(spec.calls, 0)  # never opened as a document
        _, counts = self.run_export(data)
        self.assertEqual(counts, dict(exported=0, up_to_date=1, files=0, files_up_to_date=4, skipped=0, failed=0))
        self.assertEqual(len(spec.downloads), 1)
        path.write_bytes(b'damaged')
        _, counts = self.run_export(data)
        self.assertEqual((counts['files'], len(spec.downloads)), (1, 2))
        self.assertEqual(path.read_bytes(), spec.content)

    def test_a_name_without_an_extension_gets_fusions(self):
        from fusion_bulk_export.core import file_name_parts
        self.assertEqual(file_name_parts(fake.File('Rev 1.2', extension='pdf')), ('Rev 1.2', 'pdf'))
        self.assertEqual(file_name_parts(fake.File('Notes', extension='txt')), ('Notes', 'txt'))
        self.assertEqual(file_name_parts(fake.File('Sheet', extension='f2d')), ('Sheet', 'pdf'))
        self.assertEqual(file_name_parts(fake.File('.hidden', extension='')), ('.hidden', ''))

    def test_files_fusion_does_not_hand_out_are_skipped_with_the_reason(self):
        cases = {'false': 'Fusion did not download it', 'raise': 'Synthetic download failure',
                 'nothing': 'wrote no file'}
        files = []
        for mode in cases:
            file = fake.File('File %s.pdf' % mode, mode, extension='pdf')
            file.download_mode = mode
            files.append(file)
        boards = [fake.File('Board', 'board', extension='fbrd'), fake.File('Schematic', 'sch', extension='fsch')]
        _, counts = self.run_export(fake.data(files + boards))
        self.assertEqual(counts, dict(exported=0, up_to_date=0, files=0, files_up_to_date=0, skipped=5, failed=0))
        entries = {e['id']: e for e in self.manifest['entries']}
        for mode, reason in cases.items():
            self.assertEqual(entries[mode]['status'], 'skipped')
            self.assertIn(reason, entries[mode]['error'])
            self.assertEqual(entries[mode]['files'], [])
        self.assertEqual(entries['board']['error'], 'Fusion cannot export this file type through its API (.fbrd)')
        self.assertEqual([b.downloads for b in boards], [[], []])  # not even asked: no extra API calls
        self.assertEqual([p.name for p in (self.root / 'Hub' / 'Project').iterdir()], [])  # nothing left over
        for file in files:
            file.download_mode = 'ok'
        _, counts = self.run_export(fake.data(files + boards))  # skips are tried again
        self.assertEqual((counts['files'], counts['skipped']), (3, 2))

    def test_a_drawing_is_saved_as_its_pdf(self):
        drawing = fake.File('Sheet A', 'drawing', extension='f2d', references=2)
        drawing.products_flaky = 1  # the first read right after opening fails, as seen on real Fusion
        with patch('fusion_bulk_export.fusion.time.sleep'):
            _, counts = self.run_export(fake.data([drawing]))
        entry = self.manifest['entries'][0]
        self.assertEqual((counts['files'], entry['kind'], entry['status']), (1, 'file', 'done'))
        self.assertEqual(entry['files'][0]['path'], 'Hub/Project/Sheet A.pdf')
        self.assertTrue((self.root / 'Hub/Project/Sheet A.pdf').read_bytes().startswith(b'%PDF'))
        self.assertEqual(self.app.documents.items, [])  # the drawing and what it pulled in are closed
        self.assertEqual(drawing.downloads, [])
        _, counts = self.run_export(fake.data([drawing]))
        self.assertEqual((counts['up_to_date'], counts['files_up_to_date']), (0, 1))

    def test_a_drawing_pdf_written_late_is_waited_for(self):
        drawing = fake.File('Sheet', 'drawing', extension='f2d')
        drawing.pdf_mode = 'later'
        with patch('fusion_bulk_export.fusion.adsk.doEvents', fake.do_events, create=True), \
                patch('fusion_bulk_export.fusion.time.sleep'):
            _, counts = self.run_export(fake.data([drawing]))
        self.assertEqual(counts['files'], 1)

    def test_a_drawing_pdf_that_never_appears_is_a_skip_and_not_retried_this_run(self):
        from fusion_bulk_export import fusion
        first, second = fake.File('One', 'one', extension='f2d'), fake.File('Two', 'two', extension='f2d')
        refused = fake.File('Three', 'three', extension='f2d')
        first.pdf_mode = second.pdf_mode = 'never'
        with patch.object(fusion, 'PDF_WAIT_SECONDS', 0.05), patch('fusion_bulk_export.fusion.time.sleep'):
            _, counts = self.run_export(fake.data([first, second]))
        self.assertEqual(counts['skipped'], 2)
        self.assertEqual([e['error'] for e in self.manifest['entries']], ['Fusion did not write the drawing PDF'] * 2)
        self.assertEqual((first.calls, second.calls), (1, 0))  # the second was not opened: no wait
        self.assertEqual(self.app.documents.items, [])
        refused.pdf_mode = 'false'
        self.adapter.pdf_broken = None
        self.run_export(fake.data([refused]))
        self.assertIn('Fusion cannot export this file type through its API (.f2d',
                      self.manifest['entries'][-1]['error'])

    def test_full_disk_while_saving_a_file_stops_the_run(self):
        a, b = fake.File('A.pdf', 'a', extension='pdf'), fake.File('B.pdf', 'b', extension='pdf')

        def full(path, handler):
            raise OSError(errno.ENOSPC, 'No space left on device')
        a.download = full
        exporter, counts = self.run_export(fake.data([a, b]))
        self.assertEqual(exporter.run_info['stopped_code'], 'disk_full')
        self.assertEqual((counts['failed'], b.downloads), (1, []))

    def test_same_named_files_and_a_file_named_like_an_export(self):
        one, two = fake.File('Spec', 'one', extension='pdf'), fake.File('spec', 'two', extension='pdf')
        other_type = fake.File('Spec', 'three', extension='txt')
        design = fake.File('Bracket', 'bracket')
        lookalike = fake.File('Bracket v3', 'lookalike', extension='step')
        data = fake.data([one, two, other_type, design, lookalike])
        self.run_export(data)
        paths = {e['id']: [f['path'] for f in e['files']] for e in self.manifest['entries']}
        self.assertEqual(paths['one'], ['Hub/Project/Spec.pdf'])
        self.assertEqual(paths['two'], ['Hub/Project/spec~%s.pdf' % names.tag('file:two')])
        self.assertEqual(paths['three'], ['Hub/Project/Spec.txt'])  # another extension: no clash
        self.assertEqual(paths['lookalike'], ['Hub/Project/Bracket v3~%s.step' % names.tag('file:lookalike')])
        self.assertEqual((self.root / 'Hub/Project/Bracket v3.step').read_bytes(), fake.step_text(1))
        two.versionNumber = 4
        self.run_export(data)  # names rebuilt from the manifest on the next run: the same choices
        self.assertEqual([f['path'] for e in self.manifest['entries'] if e['id'] == 'two' for f in e['files']],
                         ['Hub/Project/spec~%s.pdf' % names.tag('file:two')])

    def test_a_new_version_of_a_file_replaces_the_old_one(self):
        spec = fake.File('Spec', 'spec', extension='pdf')
        self.run_export(fake.data([spec]))
        spec.versionNumber, spec.content = 4, b'version four'
        _, counts = self.run_export(fake.data([spec]))
        self.assertEqual(counts['files'], 1)
        self.assertEqual([(e['version'], e['files'][0]['path']) for e in self.manifest['entries']],
                         [(4, 'Hub/Project/Spec.pdf')])
        self.assertEqual((self.root / 'Hub/Project/Spec.pdf').read_bytes(), b'version four')
        # Both versions in the manifest (a run that stopped before saving it): the older one goes.
        manifest = json.loads((self.root / 'manifest.json').read_text())
        older = dict(manifest['entries'][0], version=3, key='["old"]')
        manifest['entries'].insert(0, older)
        (self.root / 'manifest.json').write_text(json.dumps(manifest))
        self.assertEqual([e['version'] for e in Exporter(self.root, self.adapter).manifest['entries']], [4])

    def test_designs_only_skips_other_files(self):
        spec = fake.File('Spec', 'spec', extension='pdf')
        exporter, counts = self.run_export(fake.data([spec, fake.File()]), other_files=False)
        self.assertEqual(counts, dict(exported=1, up_to_date=0, files=0, files_up_to_date=0, skipped=1, failed=0))
        self.assertEqual(spec.downloads, [])
        self.assertFalse(exporter.run_info['other_files'])
        self.assertEqual(self.manifest['entries'][0]['error'], 'Not a Fusion design: pdf')

    def test_check_folder_covers_other_files(self):
        from fusion_bulk_export import checks
        spec = fake.File('Spec', 'spec', extension='pdf')
        self.run_export(fake.data([spec, fake.File()]))
        totals = checks.check_folder(self.root)
        self.assertEqual((totals['designs'], totals['other_files'], totals['failed_checks']), (1, 1, 0))
        (self.root / 'Hub/Project/Spec.pdf').write_bytes(b'Synthetic file content of spe!')
        totals = checks.check_folder(self.root)
        self.assertEqual((totals['changed'], totals['failed_checks']), (1, 1))

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

    def test_fusion_writes_to_short_staging_paths_then_files_move(self):
        file = fake.File('y' * 100)
        folders = [fake.folder('x' * 100, files=[file])]
        folders = [fake.folder('z' * 100, folders=folders)]
        given = []
        original = fake.Manager.execute
        with patch.object(fake.Manager, 'execute', lambda m, path: given.append(path) or original(m, path)):
            self.run_export(fake.data(folders=folders))
        entry = self.manifest['entries'][0]
        self.assertEqual((entry['status'], entry['shortened']), ('done', False))
        final = self.root / entry['files'][0]['path']
        self.assertGreater(len(str(final)), 300)
        self.assertTrue(final.is_file())
        self.assertTrue(all(Path(path).parent == self.root / '.staging' and len(path) < 260 for path in given))
        self.assertTrue((self.root / entry['thumbnail']['path']).is_file())
        self.assertFalse((self.root / '.staging').exists())  # emptied at the end of the run

    def test_fake_fusion_refuses_long_or_prefixed_paths(self):
        manager = fake.Manager(fake.File())
        for path in ('/' + 'x' * 300 + '.step', '\\\\?\\C:\\a.step'):
            with self.assertRaises(AssertionError):
                manager.execute(path)

    def test_deep_output_folder_stages_in_the_temporary_folder(self):
        from fusion_bulk_export import staging
        deep = self.root / ('d' * 120) / ('e' * 120)
        temporary = self.root / 'tmp'
        with patch.object(staging.tempfile, 'gettempdir', return_value=str(temporary)):
            exporter = Exporter(deep, self.adapter)
            self.assertEqual(exporter.staging.folder.parent, temporary)
            exporter.run(fake.data([fake.File()]))
        manifest = json.loads((deep / 'manifest.json').read_text())
        self.assertEqual(manifest['entries'][0]['status'], 'done')
        self.assertFalse(exporter.staging.folder.exists())

    def test_staged_files_cross_disks_by_copy_and_leftovers_are_cleaned(self):
        from fusion_bulk_export import staging
        (self.root / '.staging').mkdir()
        (self.root / '.staging' / 'left-over.f3d').write_bytes(b'from a stopped run')
        real = os.replace

        def other_disk(source, target):
            if '.staging' in str(source):
                raise OSError(errno.EXDEV, 'Invalid cross-device link')
            return real(source, target)
        with patch.object(staging.os, 'replace', other_disk):
            self.run_export(fake.data([fake.File()]))
        self.assertEqual(self.manifest['entries'][0]['status'], 'done')
        self.assertFalse((self.root / '.staging').exists())

    def test_fusion_path_refuses_long_and_prefixed_paths(self):
        self.assertEqual(fusion_path('C:\\a', windows=True), 'C:\\a')
        for bad in ('C:\\' + 'x' * 300, '\\\\?\\C:\\a'):
            with self.assertRaises(ValueError):
                fusion_path(bad)

    def test_long_path_prefix(self):
        self.assertEqual(long_path('C:\\out\\a', windows=True), '\\\\?\\C:\\out\\a')
        self.assertEqual(long_path('C:/out/a', windows=True), '\\\\?\\C:\\out\\a')
        self.assertEqual(long_path('\\\\server\\share\\a', windows=True), '\\\\?\\UNC\\server\\share\\a')
        self.assertEqual(long_path('\\\\?\\C:\\a', windows=True), '\\\\?\\C:\\a')
        self.assertEqual(long_path('relative\\a', windows=True), 'relative\\a')
        self.assertEqual(long_path('/tmp/a', windows=False), '/tmp/a')
        self.assertFalse(names.too_long('C:\\' + 'x' * 300, 'win32'))  # Python writes long final paths

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
        self.assertEqual(counts, dict(exported=1, up_to_date=1, files=0, files_up_to_date=0, skipped=0, failed=0))
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

    def test_links_recorded_while_exporting(self):
        plain, assembly = fake.File('Plain', 'plain'), fake.File('Assembly', 'assembly', references=2)
        assembly.hasOutofDateChildReferences = True
        _, counts = self.run_export(fake.data([plain, assembly]))
        self.assertEqual(counts['exported'], 2)
        entries = {e['id']: e for e in self.manifest['entries']}
        self.assertEqual((entries['plain']['has_references'], entries['plain']['references_out_of_date']),
                         (False, False))
        self.assertEqual((entries['assembly']['has_references'], entries['assembly']['references_out_of_date']),
                         (True, True))
        self.assertNotIn('fusion_error', entries['assembly'])

    def test_missing_link_fails_with_a_plain_reason_and_keeps_fusions_text(self):
        assembly = fake.File('Assembly', 'assembly', references=2)
        assembly.open_error = '5 : Reference not resolved'
        _, counts = self.run_export(fake.data([assembly]))
        self.assertEqual(counts['failed'], 1)
        entry = self.manifest['entries'][0]
        self.assertEqual(entry['error'], 'A linked design is missing or cannot be opened (2 links).')
        self.assertEqual(entry['fusion_error'], '5 : Reference not resolved')
        self.assertEqual(entry['attempts'], 2)  # still attempted as usual
        self.assertEqual(assembly.calls, 2)
        # Fixed in Fusion: the next run exports it and the link reason goes.
        assembly.open_error = None
        self.run_export(fake.data([assembly]))
        entry = self.manifest['entries'][0]
        self.assertEqual((entry['status'], entry['error']), ('done', None))
        self.assertNotIn('fusion_error', entry)

    def test_design_without_links_keeps_fusions_error(self):
        part = fake.File('Part', 'part')
        part.open_error = 'Synthetic open failure'
        self.run_export(fake.data([part]))
        entry = self.manifest['entries'][0]
        self.assertEqual(entry['error'], 'Synthetic open failure')
        self.assertNotIn('fusion_error', entry)

    def test_open_with_edits_is_not_called_a_link_problem(self):
        assembly = fake.File('Assembly', 'assembly', references=1)
        mine = fake.Document(assembly, self.app.documents)
        mine.isModified = True
        self.app.documents.items.append(mine)
        self.run_export(fake.data([assembly]))
        self.assertIn('unsaved changes', self.manifest['entries'][0]['error'])

    def test_link_facts_read_defensively(self):
        class Unreadable(fake.File):
            @property
            def hasOutofDateChildReferences(self):
                raise RuntimeError('2 : not available')

            @hasOutofDateChildReferences.setter
            def hasOutofDateChildReferences(self, value):
                pass

            @property
            def childReferences(self):
                raise RuntimeError('3 : cannot list')

            @childReferences.setter
            def childReferences(self, value):
                pass
        odd = Unreadable('Odd', 'odd')
        del odd.hasChildReferences  # an older Fusion without the property
        _, counts = self.run_export(fake.data([odd]))
        self.assertEqual(counts['exported'], 1)  # reading links never stops an export
        entry = self.manifest['entries'][0]
        self.assertEqual((entry['has_references'], entry['references_out_of_date'], entry['references']),
                         (None, None, None))
        odd.open_error = 'Synthetic open failure'
        self.root = Path(tempfile.mkdtemp(dir=self.temp.name))
        self.run_export(fake.data([odd]))
        self.assertEqual(self.manifest['entries'][0]['error'], 'Synthetic open failure')

    def test_link_reason_counts(self):
        self.assertEqual(core.link_reason(1), 'A linked design is missing or cannot be opened (1 link).')
        self.assertEqual(core.link_reason(0), 'A linked design is missing or cannot be opened.')

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

    def test_linked_documents_closed_when_export_fails_and_shown_ones_left(self):
        assembly = fake.File('Assembly', 'assembly', references=2)
        assembly.childReferences.item(1).modified_on_open = True  # marked modified by Fusion, hidden
        original = fake.Documents.open
        shown = fake.Document(fake.File('Opened meanwhile', 'meanwhile'), None)
        shown.isModified = True

        def open_and_user_opens_one(documents, file, visible):
            document = original(documents, file, visible)
            if shown not in documents.items:
                shown.owner = documents
                documents.items.append(shown)  # the user opened a design during the export
            return document
        with patch.object(fake.Manager, 'execute', return_value=False), \
                patch.object(fake.Documents, 'open', open_and_user_opens_one):
            self.run_export(fake.data([assembly]))
        entry = self.manifest['entries'][0]
        self.assertEqual(entry['status'], 'failed')
        self.assertEqual(assembly.calls, 2)  # two attempts
        self.assertEqual(self.app.documents.items, [shown])
        self.assertFalse(shown.closed)

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
        self.assertEqual(counts, dict(exported=0, up_to_date=0, files=0, files_up_to_date=0, skipped=0, failed=5))
        self.assertEqual([f.calls for f in files], [2] * 5 + [0, 0])
        self.assertIn('5 designs in a row failed', exporter.run_info['stopped_reason'])
        self.assertEqual(exporter.run_info['stopped_code'], 'failing')
        self.assertFalse(exporter.run_info['cancelled'])
        self.assertEqual(self.manifest['runs'][-1]['stopped_code'], 'failing')
        for file in files:
            file.failures = 0  # back online
        exporter, counts = self.run_export(fake.data(files))
        self.assertEqual(counts['exported'], 7)
        self.assertIsNone(exporter.run_info['stopped_reason'])

    def test_a_success_resets_the_failure_count(self):
        files = self.failing_files(4) + [fake.File(identity='good')] + self.failing_files(4)
        exporter, counts = self.run_export(fake.data(files))
        self.assertEqual(counts, dict(exported=1, up_to_date=0, files=0, files_up_to_date=0, skipped=0, failed=8))
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
        self.assertEqual(counts['exported'], 1)  # a warning, not a stop

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
        self.assertEqual(counts, dict(exported=1, up_to_date=5, files=0, files_up_to_date=0, skipped=0, failed=0))
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
        self.assertEqual(exporter.run_info['counts']['exported'], 5000)
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
        self.assertEqual(counts["up_to_date"], 3)
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
