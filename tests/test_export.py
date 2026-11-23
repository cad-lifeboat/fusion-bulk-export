import csv
import errno
import io
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
from fusion_bulk_export import core, names, power, report
from fusion_bulk_export.core import Exporter, Selection, atomic_write, free_bytes, sanitize, walk
from fusion_bulk_export.names import clean, fusion_path, long_path

fake.install()
from fusion_bulk_export.fusion import FusionAdapter


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        # Resolved like the Exporter's own root: on macOS the temporary folder is a symlink
        # (/var -> /private/var) and on Windows it may be an 8.3 short name (RUNNER~1).
        self.root = Path(self.temp.name).resolve()
        self.app = NS(documents=fake.Documents())
        self.adapter = FusionAdapter(self.app)

    def run_export(self, data, **kwargs):
        exporter = Exporter(self.root, self.adapter)
        result = exporter.run(data, **kwargs)
        self.manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
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
        self.assertEqual(exporter.run_info['selection'], '0 whole projects, 1 folder')
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
        self.root = Path(self.temp.name).resolve()
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
        self.assertEqual(counts, dict(exported=2, up_to_date=0, files=0, files_up_to_date=0, skipped=0, unsupported=0, failed=0, linked_copies=0))
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
        self.assertEqual(self.manifest['entries'][1]['references'], [dict(id='linked-assembly', name='Linked part', version=3, path=None)])
        self.assertTrue(all(doc.closed for file in [a, b] for doc in file.documents))

    def test_each_step_is_checked_inline_and_warnings_stored(self):
        good, empty, more = fake.File('Good', 'good'), fake.File('Empty', 'empty'), fake.File('More', 'more')
        empty.step_solids = empty.step_hidden_solids = 0
        more.step_solids = 3
        # Hidden bodies are included (this Fusion offers it): every solid body of the design is
        # expected, each component once. The default design has one shown and one hidden solid.
        # Two different parts and two instances of one: Fusion shows 4 solids per occurrence, 3
        # distinct ones. A STEP with fewer than 3 is missing bodies; 3 to 8 is only noted.
        bolt, plate = fake.component('bolt', [fake.body()]), fake.component('plate', [fake.body()])
        assembly = [fake.File('Short', 'short'), fake.File('Twice', 'twice'), fake.File('Distinct', 'distinct')]
        for file, solids in zip(assembly, (2, 8, 3)):
            file.root = fake.component('root', [fake.body()], [fake.occurrence(bolt), fake.occurrence(bolt),
                                                                fake.occurrence(plate)])
            file.step_solids = solids
        _, counts = self.run_export(fake.data([good, empty, more] + assembly))
        self.assertEqual(counts['exported'], 6)  # warnings are not failures
        entries = {e['id']: e for e in self.manifest['entries']}
        self.assertEqual((entries['good']['step_solids'], entries['good']['warnings']), (2, []))
        self.assertIs(entries['good']['step_includes_hidden'], True)
        self.assertEqual(entries['empty']['warnings'], ['STEP has no bodies (2 solid bodies in the design)'])
        # More solids than Fusion showed (seen on real Fusion: about twice): recorded, no warning.
        self.assertEqual((entries['more']['step_solids'], entries['more']['warnings']), (4, []))
        self.assertEqual((entries['twice']['visible_solid_bodies'], entries['twice']['visible_solid_bodies_distinct'],
                          entries['twice']['step_solids'], entries['twice']['warnings']), (4, 3, 8, []))
        self.assertEqual(entries['distinct']['warnings'], [])
        self.assertEqual(entries['short']['warnings'], ['STEP has 2 solids, fewer than the 3 distinct ones in the design'])

    def test_broken_archive_fails_and_is_retried_a_truncated_step_keeps_the_archive(self):
        for broken in ('step', 'archive'):
            with self.subTest(broken=broken):
                self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
                self.addCleanup(self.temp.cleanup)
                self.root = Path(self.temp.name).resolve()
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
                if broken == 'archive':
                    self.assertEqual((entry['status'], entry['attempts']), ('failed', 2))
                    self.assertIn('damaged', entry['error'])
                else:
                    # The .f3d is kept; the incomplete STEP is removed and tried again next run.
                    self.assertEqual((entry['status'], entry['step']), ('done', 'failed'))
                    self.assertIn('incomplete', entry['step_error'])
                    self.assertEqual([f['role'] for f in entry['files']], ['archive'])
                    self.assertFalse(list(self.root.rglob('*.step')))
                self.run_export(fake.data([file]))  # a good export next time
                entry = self.manifest['entries'][0]
                self.assertEqual(entry['status'], 'done')
                self.assertEqual(sorted(f['role'] for f in entry['files']), ['archive', 'step'])
                self.assertNotIn('step', entry)

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
        self.run_export(data, older_versions='keep')
        self.assertEqual(len(self.manifest['entries']), 2)
        self.assertTrue(all((self.root / path).exists() for path in previous))
        self.assertTrue(all(' v4.' in info['path'] for info in self.manifest['entries'][1]['files']))

    def test_new_version_moves_previous_pair_to_older_versions_by_default(self):
        file = fake.File()
        data = fake.data([file])
        self.run_export(data)
        previous = [info['path'] for info in self.manifest['entries'][0]['files']]
        file.versionNumber = 4
        self.run_export(data)
        self.assertEqual(self.manifest['entries'][0]['status'], 'moved')
        self.assertFalse(any((self.root / path).exists() for path in previous))
        self.assertTrue(all((self.root / 'Older versions' / path).is_file() for path in previous))

    def test_unreadable_manifest_is_preserved(self):
        manifest = self.root / 'manifest.json'
        manifest.write_text('{incomplete', encoding='utf-8')
        with self.assertRaises(json.JSONDecodeError):
            Exporter(self.root, self.adapter)
        self.assertEqual(manifest.read_text(encoding='utf-8'), '{incomplete')

    def test_a_step_fusion_will_not_write_keeps_the_archive_with_a_warning(self):
        # Earlier versions failed the whole design and threw its .f3d away.
        for how in ('false', 'raises', 'no file'):
            with self.subTest(how=how):
                self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
                self.addCleanup(self.temp.cleanup)
                self.root = Path(self.temp.name).resolve()
                file = fake.File()
                original = fake.Manager.execute

                def execute(manager, path):
                    if not path.endswith('.step'):
                        return original(manager, path)
                    if how == 'raises':
                        raise RuntimeError('3 : STEP translator failed')
                    return how == 'no file'
                with patch.object(fake.Manager, 'execute', execute):
                    _, counts = self.run_export(fake.data([file]))
                entry = self.manifest['entries'][0]
                self.assertEqual((entry['status'], entry['step'], entry['attempts']), ('done', 'failed', 1))
                self.assertEqual([f['role'] for f in entry['files']], ['archive'])
                self.assertTrue((self.root / entry['files'][0]['path']).is_file())
                self.assertIn(core.STEP_FAILED_WARNING, entry['warnings'])
                self.assertEqual({'false': 'STEP export returned false', 'raises': '3 : STEP translator failed',
                                  'no file': 'Fusion reported a successful STEP export but wrote no file'}[how],
                                 entry['step_error'])
                self.assertEqual((counts['exported'], counts['failed']), (1, 0))
                self.assertTrue(all(doc.closed for doc in file.documents))
                self.assertFalse(list(self.root.rglob('*.step')))
                page = report.render_html(self.manifest, None, counts)
                self.assertIn('Warning: Saved, but Fusion could not write its STEP file', page)
                self.assertEqual(report.warned(self.manifest), 1)
                # Not up to date: the next run tries again and adds the STEP.
                self.run_export(fake.data([file]))
                entry = self.manifest['entries'][0]
                self.assertEqual(file.calls, 2)
                self.assertEqual(sorted(f['role'] for f in entry['files']), ['archive', 'step'])
                self.assertEqual(entry['warnings'], [])
                self.assertNotIn('step_error', entry)

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
        self.assertIn('verified', (self.root / 'export.log').read_text(encoding='utf-8'))

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
        manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
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
        self.assertEqual((counts['skipped'], counts['unsupported']), (2, 1))
        self.assertEqual((files[0].calls, files[0].downloads), (0, []))
        self.assertEqual([e['kind'] for e in self.manifest['entries']], ['file', 'design', 'file'])
        self.assertEqual([e['status'] for e in self.manifest['entries']], ['unsupported', 'skipped', 'skipped'])
        self.assertTrue(all(e['error'] for e in self.manifest['entries']))

    def test_other_files_are_saved_under_their_own_names(self):
        # As on real Fusion, an uploaded file's name already holds its extension.
        spec, manual = fake.File('Spec.pdf', 'spec', extension='pdf'), fake.File('Manual.PDF', 'manual', extension='pdf')
        picture = fake.File('Photo.jpg', 'photo', extension='jpg')
        script = fake.File('notes.scr', 'script', extension='htm')  # fileExtension can disagree: trust the name
        design = fake.File('Part', 'part')
        data = fake.data([design], [fake.folder('Docs', [spec, manual, picture, script], identity='docs')])
        _, counts = self.run_export(data)
        self.assertEqual(counts, dict(exported=1, up_to_date=0, files=4, files_up_to_date=0, skipped=0, unsupported=0, failed=0, linked_copies=0))
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
        self.assertEqual(counts, dict(exported=0, up_to_date=1, files=0, files_up_to_date=4, skipped=0, unsupported=0, failed=0, linked_copies=0))
        self.assertEqual(len(spec.downloads), 1)
        path.write_bytes(b'damaged')
        _, counts = self.run_export(data)
        self.assertEqual((counts['files'], len(spec.downloads)), (1, 2))
        self.assertEqual(path.read_bytes(), spec.content)

    def test_a_name_without_an_extension_gets_fusions(self):
        from fusion_bulk_export.core import file_name_parts
        self.assertEqual(file_name_parts(fake.File('Rev 1.2', extension='pdf')), ('Rev 1.2', 'pdf'))
        self.assertEqual(file_name_parts(fake.File('Notes', extension='txt')), ('Notes', 'txt'))
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
        # Electronics types are "not supported", apart from skips and failures.
        self.assertEqual(counts, dict(exported=0, up_to_date=0, files=0, files_up_to_date=0, skipped=3,
                                      unsupported=2, failed=0, linked_copies=0))
        entries = {e['id']: e for e in self.manifest['entries']}
        self.assertEqual((entries['board']['status'], entries['sch']['status']), ('unsupported', 'unsupported'))
        for mode, reason in cases.items():
            self.assertEqual(entries[mode]['status'], 'skipped')
            self.assertIn(reason, entries[mode]['error'])
            self.assertEqual(entries[mode]['files'], [])
        self.assertEqual(entries['board']['error'],
                         'Electronics file (.fbrd) not exported: Fusion offers no export for it to add-ins.')
        self.assertEqual([b.downloads for b in boards], [[], []])  # not even asked: no extra API calls
        self.assertEqual([p.name for p in (self.root / 'Hub' / 'Project').iterdir()], [])  # nothing left over
        for file in files:
            file.download_mode = 'ok'
        _, counts = self.run_export(fake.data(files + boards))  # skips are tried again
        self.assertEqual((counts['files'], counts['skipped'], counts['unsupported']), (3, 0, 2))
        # Designs only: an electronics file is still "not supported", not a skip of the user's choice.
        _, counts = self.run_export(fake.data(boards), other_files=False)
        self.assertEqual((counts['skipped'], counts['unsupported']), (0, 2))

    def test_drawings_are_never_opened_and_are_not_supported(self):
        # Exporting a drawing's PDF crashed Fusion on real machines: no code path may open one.
        drawing = fake.File('Sheet A', 'drawing', extension='f2d', references=2)
        design = fake.File('Part', 'part')
        _, counts = self.run_export(fake.data([drawing, design]))
        entry = next(e for e in self.manifest['entries'] if e['id'] == 'drawing')
        self.assertEqual((entry['kind'], entry['status'], entry['files']), ('file', 'unsupported', []))
        self.assertEqual(entry['error'], "Drawing not exported: Fusion's drawing export crashes when an add-in runs it.")
        self.assertEqual((counts['exported'], counts['unsupported'], counts['skipped'], counts['failed']),
                         (1, 1, 0, 0))
        self.assertEqual((drawing.calls, drawing.downloads), (0, []))  # neither opened nor downloaded
        self.assertEqual(self.app.documents.items, [])
        # The same with only designs asked for, and on every later run.
        _, counts = self.run_export(fake.data([drawing]), other_files=False)
        self.assertEqual((counts['unsupported'], drawing.calls), (1, 0))

    def test_the_adapter_refuses_to_open_or_download_a_drawing(self):
        from fusion_bulk_export.core import NotSaved
        drawing = fake.File('Sheet', 'sheet', extension='f2d')
        with self.assertRaises(RuntimeError):
            self.adapter.export(drawing, self.root / 'Sheet v1')
        with self.assertRaises(NotSaved):
            self.adapter.download(drawing, self.root / 'Sheet.pdf')
        self.assertEqual((drawing.calls, drawing.downloads), (0, []))
        with self.assertRaises(fake.FusionCrashed):  # what the fake does if anything ever tries
            self.app.documents.open(drawing, False)

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
        # The design's STEP: its shown solid and its hidden one (hidden bodies are included).
        self.assertEqual((self.root / 'Hub/Project/Bracket v3.step').read_bytes(), fake.step_text(2))
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
        manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        older = dict(manifest['entries'][0], version=3, key='["old"]')
        manifest['entries'].insert(0, older)
        (self.root / 'manifest.json').write_text(json.dumps(manifest))
        self.assertEqual([e['version'] for e in Exporter(self.root, self.adapter).manifest['entries']], [4])

    def test_designs_only_skips_other_files(self):
        spec = fake.File('Spec', 'spec', extension='pdf')
        exporter, counts = self.run_export(fake.data([spec, fake.File()]), other_files=False)
        self.assertEqual(counts, dict(exported=1, up_to_date=0, files=0, files_up_to_date=0, skipped=1, unsupported=0, failed=0, linked_copies=0))
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
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')), {'before': True})
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
        for _ in range(4):  # deep enough with Windows' 64-character names too
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
        entry = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))['entries'][0]
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
        for _ in range(3):  # deep enough with Windows' 64-character names too
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
        # Deep enough that "<deep>/.staging" is over staging.MAX_FOLDER characters, yet within
        # Windows' 200-character limit for the output folder itself.
        deep = self.root / 'd' / ('e' * (names.OUTPUT_ROOT_LIMIT - 3 - len(str(self.root))))
        self.assertGreater(len(str(deep / staging.NAME)), staging.MAX_FOLDER)
        self.assertIsNone(names.output_root_problem(deep, 'win32'))
        temporary = self.root / 'tmp'
        with patch.object(staging.tempfile, 'gettempdir', return_value=str(temporary)):
            exporter = Exporter(deep, self.adapter)
            self.assertEqual(exporter.staging.folder.parent, temporary)
            exporter.run(fake.data([fake.File()]))
        manifest = json.loads((deep / 'manifest.json').read_text(encoding='utf-8'))
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
        self.assertEqual(counts, dict(exported=1, up_to_date=1, files=0, files_up_to_date=0, skipped=0, unsupported=0, failed=0, linked_copies=0))
        self.assertEqual(old.calls, 0)
        self.assertEqual(self.manifest['schema'], 2)
        self.assertEqual(self.manifest['entries'][0], entry)  # untouched, files where they were
        self.assertIn('Hub/Project/New v3.f3d', self.paths())
        # Two layouts in one folder: the report and the summary say so.
        from fusion_bulk_export import report
        exporter = Exporter(self.root, self.adapter)
        self.assertTrue(exporter.manifest['older_layout'])
        self.assertFalse(exporter.upgraded)  # already schema 2 now: the summary note was given once
        page = report.render_html(exporter.manifest, exporter.run_info, {})
        self.assertIn('Older exports keep their old folder names; new items go in readable folders.', page)
        upgraded = dict(exporter.run_info, upgraded_schema=1)
        self.assertIn(report.OLDER_LAYOUT, report.summary({}, upgraded, exporter.manifest, self.root)['notes'])
        self.assertNotIn(report.OLDER_LAYOUT, report.summary({}, exporter.run_info, exporter.manifest,
                                                             self.root)['notes'])
        with self.assertRaises(ValueError):
            (self.root / 'manifest.json').write_text(json.dumps(dict(schema=3, runs=[], entries=[])))
            Exporter(self.root, self.adapter)

    def test_windows_output_root_over_200_characters_is_refused_with_a_plain_reason(self):
        from fusion_bulk_export import errors
        long = self.root / ('x' * 120) / ('y' * 120)
        with patch.object(core, 'sys', NS(platform='win32')):
            with self.assertRaises(ValueError) as refused:
                Exporter(long, self.adapter)
        self.assertIn('on Windows it can be at most 200', str(refused.exception))
        self.assertFalse(long.exists())  # nothing was created
        shown = errors.output_folder(refused.exception, long)
        self.assertIn('on Windows it can be at most 200', shown.text())
        self.assertIsNone(names.output_root_problem(long, 'darwin'))
        self.assertIsNone(names.output_root_problem('C:\\' + 'z' * 197, 'win32'))

    def test_output_folder_rules(self):
        deep = self.root / ('o' * 120)  # within Windows' 200 characters for the output folder
        exporter = Exporter(deep, self.adapter)  # no 100-character cap any more
        self.assertEqual(exporter.root, deep)
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

    def test_unreadable_links_still_export_with_a_warning(self):
        # Seen on real Fusion: reading a DataFile's references raised at once on both attempts,
        # before the design was opened, and failed the whole design.
        odd, plain = fake.UnreadableLinks('Odd', 'odd'), fake.File('Plain', 'plain')
        with self.assertRaises(RuntimeError):
            odd.hasChildReferences
        exporter, counts = self.run_export(fake.data([odd, plain]))
        self.assertEqual((counts['exported'], counts['failed'], counts['skipped']), (2, 0, 0))
        entry = next(e for e in self.manifest['entries'] if e['id'] == 'odd')
        self.assertEqual((entry['status'], entry['attempts'], odd.calls), ('done', 1, 1))
        self.assertTrue(entry['references_unreadable'])
        self.assertEqual(entry['references_error'], fake.RELATIONSHIPS)
        self.assertEqual((entry['has_references'], entry['references']), (None, None))
        self.assertNotIn('references_unreadable', next(e for e in self.manifest['entries'] if e['id'] == 'plain'))
        from fusion_bulk_export import report
        text = report.summary_text(counts, exporter.run_info, self.manifest, self.root)
        self.assertIn('Warnings      1', text)
        page = report.render_html(self.manifest, exporter.run_info, counts)
        self.assertIn('Warning: Links to other designs could not be read; the design itself was exported.', page)
        self.assertIn('1 whose links could not be read', page)
        import csv
        import io
        rows = {r['design']: r for r in csv.DictReader(io.StringIO(report.render_csv(self.manifest, exporter.run_info)))}
        self.assertEqual((rows['Odd']['links_unreadable'], rows['Plain']['links_unreadable']), ('yes', ''))

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
                self.root = Path(self.temp.name).resolve()
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
        self.assertEqual(counts, dict(exported=0, up_to_date=0, files=0, files_up_to_date=0, skipped=0, unsupported=0, failed=5, linked_copies=0))
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
        self.assertEqual(counts, dict(exported=1, up_to_date=0, files=0, files_up_to_date=0, skipped=0, unsupported=0, failed=8, linked_copies=0))
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
        manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['entries'], [])
        journal = (self.root / 'manifest.journal').read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(journal), 6)
        with (self.root / 'manifest.journal').open('a') as stream:
            stream.write('{"entry": {"key": "cut sh')  # the line being written when it crashed
        exporter, counts = self.run_export(fake.data(files))
        self.assertEqual(exporter.run_info['journal_replayed'], 6)
        self.assertEqual(counts, dict(exported=1, up_to_date=5, files=0, files_up_to_date=0, skipped=0, unsupported=0, failed=0, linked_copies=0))
        self.assertEqual([f.calls for f in files], [1] * 5 + [3])
        self.assertEqual(len(self.manifest['entries']), 6)
        self.assertFalse((self.root / 'manifest.journal').exists())

    def test_a_run_after_fusion_crashed_mid_export_needs_no_user_action(self):
        # Fusion died in the middle of a run: its export.lock (the dead process's id) and a
        # half-written .staging are left behind. The next run clears both by itself.
        import socket
        import time
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        (self.root / 'export.lock').write_text(json.dumps(dict(pid=dead.pid, host=socket.gethostname(),
                                                               started=time.time() - 60)))
        (self.root / '.staging').mkdir()
        (self.root / '.staging' / 'a1b2c3.f3d').write_bytes(b'half written')
        (self.root / 'manifest.journal').write_text('{"entry": {"key": "cut sho')  # cut short by the crash
        _, counts = self.run_export(fake.data([fake.File()]))
        self.assertEqual(counts['exported'], 1)
        self.assertFalse((self.root / 'export.lock').exists())
        self.assertFalse((self.root / '.staging').exists())
        self.assertEqual(self.manifest['entries'][0]['status'], 'done')

    def test_slow_items_are_recorded_and_a_slow_failure_is_not_retried(self):
        # Seen on real Fusion: opening a design "locked in another team" blocked it for ~135 s.
        from fusion_bulk_export import report
        quick, slow, stuck = fake.File('Quick', 'quick'), fake.File('Locked', 'locked'), fake.File('Stuck', 'stuck')
        slow.open_seconds = 135
        stuck.open_seconds, stuck.failures = 130, 2  # both attempts would fail and block as long
        exporter = Exporter(self.root, self.adapter)
        exporter.clock = fake.clock
        lines = []
        exporter.log = lambda message: lines.append(message)
        counts = exporter.run(fake.data([quick, slow, stuck]))
        entries = {e['id']: e for e in exporter.manifest['entries']}
        self.assertNotIn('slow_seconds', entries['quick'])
        self.assertEqual(entries['locked']['slow_seconds'], 135)
        self.assertEqual(entries['locked']['status'], 'done')
        self.assertEqual((entries['stuck']['status'], entries['stuck']['attempts'], stuck.calls), ('failed', 1, 1))
        self.assertEqual(entries['stuck']['slow_seconds'], 130)
        self.assertEqual(counts['failed'], 1)
        # The line naming the item is written before Fusion is asked to open it.
        started = [line for line in lines if line.startswith('exporting ')]
        self.assertEqual(len(started), 3)
        self.assertLess(lines.index(started[1]), next(i for i, line in enumerate(lines) if line.startswith('slow (135 s)')))
        page = report.render_html(exporter.manifest, exporter.run_info, counts)
        self.assertIn('<h2>Slow items</h2>', page)
        self.assertLess(page.index('Locked'), page.index('Stuck', page.index('<h2>Slow items</h2>')))
        self.assertIn('2 min 15 s', page)
        data = report.summary(counts, exporter.run_info, exporter.manifest, self.root)
        self.assertIn('2 items took Fusion over a minute each; the report lists them under \u201cSlow items\u201d.',
                      data['notes'])
        rows = {r['design']: r for r in csv.DictReader(io.StringIO(report.render_csv(exporter.manifest,
                                                                                      exporter.run_info)))}
        self.assertEqual((rows['Locked']['slow_seconds'], rows['Quick']['slow_seconds']), ('135', ''))
        # A quick failure is still tried twice.
        flaky = fake.File('Flaky', 'flaky')
        flaky.failures = 1
        exporter = Exporter(self.root, self.adapter)
        exporter.clock = fake.clock
        exporter.run(fake.data([flaky]))
        self.assertEqual(flaky.calls, 2)

    def test_limit_counts_designs_and_still_saves_the_folders_other_files(self):
        # --limit 2 once saved no other file although the folder had some: they came after the
        # second design. Other files in the folders visited up to the limit are saved.
        first = fake.File('Notes.pdf', 'notes', extension='pdf')
        a, b, c = fake.File('A', 'a'), fake.File('B', 'b'), fake.File('C', 'c')
        later = fake.File('Spec.pdf', 'spec', extension='pdf')
        board = fake.File('Board', 'board', extension='fbrd')
        deeper = fake.File('Deep.pdf', 'deep', extension='pdf')
        data = fake.data([first, a, b, c, later, board], [fake.folder('Sub', files=[deeper, fake.File('D', 'd')])])
        exporter, counts = self.run_export(data, limit=2)
        self.assertEqual((counts['exported'], counts['files'], counts['unsupported']), (2, 2, 1))
        ids = sorted(e['id'] for e in self.manifest['entries'])
        self.assertEqual(ids, ['a', 'b', 'board', 'notes', 'spec'])  # C and the next folder: next run
        self.assertEqual((c.calls, deeper.downloads), (0, []))
        self.assertTrue(exporter.run_info['limit_reached'])
        exporter, counts = self.run_export(data, limit=2)
        self.assertEqual((counts['exported'], counts['up_to_date'], counts['files']), (2, 2, 1))  # C, D; Deep.pdf

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
        # Speed only: 5,000 syncs are slow on some disks, and 100 full collections of this test
        # process's heap (test_memory covers the collector).
        with patch('fusion_bulk_export.journal.os.fsync'), patch.object(core, 'COLLECT_AFTER_ITEM', False):
            exporter.run(fake.data(files))
        self.assertEqual(exporter.run_info['counts']['exported'], 5000)
        self.assertLess(exporter.saves, 150)
        self.assertEqual(exporter.saves, 1 + 5000 // 50 + 1)
        manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
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
        manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        self.assertIn('error', manifest['runs'][0])
        self.assertIsNotNone(manifest['runs'][0]['finished'])


if __name__ == '__main__':
    unittest.main()
