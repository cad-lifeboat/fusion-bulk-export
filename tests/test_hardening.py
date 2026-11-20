"""Regression tests: files that must never be overwritten, documents that must never be closed,
and runs that must not stop or loop for the wrong reason."""
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace as NS

from tests import fake_adsk as fake
from fusion_bulk_export import names
from fusion_bulk_export.core import Exporter

fake.install()
from fusion_bulk_export import fusion  # noqa: E402
from fusion_bulk_export.fusion import FusionAdapter  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = NS(documents=fake.Documents())
        self.adapter = FusionAdapter(self.app)
        fusion.LEFT_OPEN.clear()
        self.addCleanup(fusion.LEFT_OPEN.clear)

    def run_export(self, data, **kwargs):
        exporter = Exporter(self.root, self.adapter)
        counts = exporter.run(data, **kwargs)
        self.manifest = json.loads((self.root / 'manifest.json').read_text())
        return exporter, counts

    def paths(self):
        return {e['id']: [f['path'] for f in e['files']] + ([e['thumbnail']['path']] if e.get('thumbnail') else [])
                for e in self.manifest['entries']}


class SharedNamesTests(Base):
    def uploads_and_design(self):
        upload = fake.File('Bracket v3', 'upload', extension='step')
        upload.content = b'USER UPLOADED STEP'
        render = fake.File('Bracket v3', 'render', extension='png')
        render.content = b'USER RENDER PNG'
        design = fake.File('Bracket', 'bracket')  # version 3: "Bracket v3.step", "Bracket v3.png"
        return upload, render, design

    def test_uploads_listed_before_a_design_keep_their_files(self):
        upload, render, design = self.uploads_and_design()
        data = fake.data([upload, render, design])
        _, counts = self.run_export(data)
        self.assertEqual((counts['exported'], counts['files']), (1, 2))
        paths = self.paths()
        self.assertEqual(paths['upload'], ['Hub/Project/Bracket v3.step'])
        self.assertEqual(paths['render'], ['Hub/Project/Bracket v3.png'])
        moved = 'Bracket~%s v3' % names.tag('file:bracket')
        self.assertEqual(paths['bracket'], ['Hub/Project/%s.f3d' % moved, 'Hub/Project/%s.step' % moved,
                                            'Hub/Project/%s.png' % moved])
        self.assertEqual((self.root / 'Hub/Project/Bracket v3.step').read_bytes(), b'USER UPLOADED STEP')
        self.assertEqual((self.root / 'Hub/Project/Bracket v3.png').read_bytes(), b'USER RENDER PNG')
        for _ in range(2):  # stable: nothing is exported or saved again
            _, counts = self.run_export(data)
            self.assertEqual(counts['exported'] + counts['files'] + counts['failed'], 0)
            self.assertEqual(self.paths(), paths)

    def test_design_listed_first_keeps_its_names_and_uploads_move_aside(self):
        upload, render, design = self.uploads_and_design()
        data = fake.data([design, upload, render])
        self.run_export(data)
        paths = self.paths()
        self.assertEqual(paths['bracket'], ['Hub/Project/Bracket v3.f3d', 'Hub/Project/Bracket v3.step',
                                            'Hub/Project/Bracket v3.png'])
        self.assertEqual(paths['render'], ['Hub/Project/Bracket v3~%s.png' % names.tag('file:render')])
        self.assertEqual((self.root / 'Hub/Project/Bracket v3.png').read_bytes(), fake.PNG)
        _, counts = self.run_export(data)
        self.assertEqual(counts['exported'] + counts['files'], 0)

    def test_a_file_nobody_recorded_is_never_written_over(self):
        folder = self.root / 'Hub' / 'Project'
        folder.mkdir(parents=True)
        (folder / 'Bracket v3.png').write_bytes(b'mine')
        (folder / 'Spec.pdf').write_bytes(b'mine too')
        self.run_export(fake.data([fake.File('Bracket', 'bracket'), fake.File('Spec.pdf', 'spec', extension='pdf')]))
        self.assertEqual((folder / 'Bracket v3.png').read_bytes(), b'mine')
        self.assertEqual((folder / 'Spec.pdf').read_bytes(), b'mine too')
        self.assertEqual(self.paths()['spec'], ['Hub/Project/Spec~%s.pdf' % names.tag('file:spec')])
        self.assertTrue(self.paths()['bracket'][0].startswith('Hub/Project/Bracket~'))

    def test_a_folder_and_a_file_of_the_same_name_stay_apart(self):
        notes = fake.File('Notes', 'notes', extension='')
        part = fake.File('Part', 'part')
        self.run_export(fake.data([notes], [fake.folder('Notes', files=[part])]))
        paths = self.paths()
        self.assertEqual(paths['notes'], ['Hub/Project/Notes'])
        self.assertTrue(paths['part'][0].startswith('Hub/Project/Notes~'), paths)

    def test_a_second_attempt_writes_the_same_names(self):
        design = fake.File('Bracket', 'bracket')
        design.failures = 1
        self.run_export(fake.data([design]))
        self.assertEqual(self.paths()['bracket'][0], 'Hub/Project/Bracket v3.f3d')

    def test_a_failed_thumbnail_leaves_the_file_at_its_path(self):
        target = self.root / 'picture.png'
        target.write_bytes(b'already here')
        staging = fusion.Staging(self.root)

        def half(path):
            Path(path).write_bytes(b'half')
            raise RuntimeError('Synthetic thumbnail failure')
        for future in (NS(state=1, dataObject=NS(saveToFile=lambda path: False)),
                       NS(state=1, dataObject=NS(saveToFile=half))):
            self.assertIsNone(fusion.save_thumbnail(future, str(target), staging))
            self.assertEqual(target.read_bytes(), b'already here')
        self.assertEqual(list(staging.folder.glob('*')), [])


class OpenDocumentTests(Base):
    def user_has_open(self, file, modified=False):
        document = fake.Document(file, self.app.documents)
        document.isModified = modified
        self.app.documents.items.append(document)
        return document

    def test_an_open_drawing_is_exported_from_and_left_open(self):
        drawing = fake.File('Sheet', 'sheet', extension='f2d')
        mine = self.user_has_open(drawing)
        _, counts = self.run_export(fake.data([drawing]))
        self.assertEqual(counts['files'], 1)
        self.assertFalse(mine.closed)
        self.assertEqual(drawing.calls, 0)
        self.assertEqual(self.app.documents.items, [mine])

    def test_an_open_drawing_with_unsaved_edits_is_never_closed(self):
        drawing = fake.File('Sheet', 'sheet', extension='f2d')
        later = fake.File('Other sheet', 'other', extension='f2d')
        mine = self.user_has_open(drawing, modified=True)
        _, counts = self.run_export(fake.data([drawing, later]))
        self.assertFalse(mine.closed)
        self.assertTrue(mine.isModified)
        entry = self.manifest['entries'][0]
        self.assertEqual(entry['status'], 'skipped')
        self.assertIn('unsaved changes', entry['error'])
        self.assertEqual(counts['files'], 1)  # the next drawing is still tried
        self.assertEqual(self.app.documents.items, [mine])

    def test_another_version_of_an_open_drawing_is_left_alone(self):
        drawing = fake.File('Sheet', 'sheet', extension='f2d')
        mine = self.user_has_open(drawing)
        mine.dataFile = NS(id='sheet', versionNumber=2)
        self.run_export(fake.data([drawing]))
        self.assertFalse(mine.closed)
        self.assertIn('Another version', self.manifest['entries'][0]['error'])

    def test_a_linked_design_marked_modified_on_open_is_closed_and_exports_later(self):
        assembly = fake.File('Assembly', 'assembly', references=1)
        child = assembly.childReferences.item(0)
        child.modified_on_open = True
        _, counts = self.run_export(fake.data([assembly, child]))
        self.assertEqual(counts['exported'], 2, self.manifest['entries'])
        self.assertEqual(self.app.documents.items, [])

    def test_a_document_fusion_will_not_close_keeps_the_export_and_is_closed_later(self):
        part, other = fake.File('Part', 'part'), fake.File('Other', 'other')
        part.close_result = False
        _, counts = self.run_export(fake.data([part]))
        self.assertEqual(counts['exported'], 1)
        entry = self.manifest['entries'][0]
        self.assertEqual((entry['status'], entry['documents_left_open']), ('done', 1))
        self.assertEqual(part.calls, 1)
        part.close_result = True
        self.adapter = FusionAdapter(self.app)  # each run has its own adapter
        part.versionNumber = 4  # a new version: the add-in's own old document is closed and reopened
        self.run_export(fake.data([part, other]))
        self.assertEqual([e['status'] for e in self.manifest['entries']], ['done', 'done', 'done'])
        self.assertEqual(self.app.documents.items, [])

    def test_a_document_left_open_is_exported_from_on_the_second_attempt(self):
        part = fake.File('Part', 'part')
        part.close_result = False
        part.failures = 1
        self.run_export(fake.data([part]))
        self.assertEqual(self.manifest['entries'][0]['status'], 'done')
        self.assertEqual(part.calls, 1)  # attempt 2 used the document attempt 1 left open


class FailingInARowTests(Base):
    def good_and_broken(self, count=5):
        files = []
        for n in range(count):
            broken = fake.File('Broken %d' % n, 'bad%d' % n)
            broken.failures = 10 ** 6  # never exports
            files += [fake.File('Good %d' % n, 'good%d' % n), broken]
        return files

    def test_designs_broken_since_an_earlier_run_never_stop_a_later_one(self):
        files = self.good_and_broken()
        exporter, counts = self.run_export(fake.data(files))
        self.assertIsNone(exporter.run_info['stopped_code'])
        new = fake.File('New design', 'new')
        for _ in range(2):  # every later run gets past them
            exporter, counts = self.run_export(fake.data(files + [new]))
            self.assertIsNone(exporter.run_info['stopped_code'])
            self.assertEqual(counts['failed'], 5)
        self.assertEqual([e['status'] for e in self.manifest['entries'] if e['id'] == 'new'], ['done'])

    def test_up_to_date_items_between_new_failures_neither_reset_nor_count(self):
        good = [fake.File('Good %d' % n, 'good%d' % n) for n in range(5)]
        self.run_export(fake.data(good))
        failing = []
        for n in range(6):
            broken = fake.File('Offline %d' % n, 'offline%d' % n)
            broken.failures = 2
            failing.append(broken)
        mixed = [item for pair in zip(good, failing) for item in pair] + failing[5:]
        exporter, counts = self.run_export(fake.data(mixed))
        self.assertEqual(exporter.run_info['stopped_code'], 'failing')  # offline: still noticed
        self.assertEqual(counts['failed'], 5)

    def test_a_design_that_opens_but_holds_no_design_resets_the_count(self):
        files = []
        for n in range(4):
            broken = fake.File('Broken %d' % n, 'bad%d' % n)
            broken.failures = 2
            files.append(broken)
        files.append(fake.File('Drawing only', 'cam', design=False))
        for n in range(4, 8):
            broken = fake.File('Broken %d' % n, 'bad%d' % n)
            broken.failures = 2
            files.append(broken)
        exporter, counts = self.run_export(fake.data(files))
        self.assertIsNone(exporter.run_info['stopped_code'])
        self.assertEqual(counts['failed'], 8)


class CountTests(Base):
    def test_limit_counts_designs_exported_in_this_run_only(self):
        first = fake.File('First', 'first')
        self.run_export(fake.data([first]))
        spec = fake.File('Spec.pdf', 'spec', extension='pdf')
        another, third = fake.File('Another', 'another'), fake.File('Third', 'third')
        _, counts = self.run_export(fake.data([first, spec, another, third]), limit=1)
        self.assertEqual((counts['up_to_date'], counts['files'], counts['exported']), (1, 1, 1))
        self.assertEqual((another.calls, third.calls), (1, 0))

    def test_other_files_up_to_date_are_counted_apart_from_designs(self):
        from fusion_bulk_export import report
        data = fake.data([fake.File('Part', 'part'), fake.File('Spec.pdf', 'spec', extension='pdf')])
        self.run_export(data)
        _, counts = self.run_export(data)
        self.assertEqual((counts['up_to_date'], counts['files_up_to_date']), (1, 1))
        self.assertIn('1 already up to date, 0 other file(s) saved, 1 other file(s) already up to date',
                      report.count_sentence(counts))


class FusionTimingTests(Base):
    def test_a_partial_drawing_pdf_is_not_saved(self):
        from unittest.mock import patch
        partial, whole = fake.File('Half', 'half', extension='f2d'), fake.File('Whole', 'whole', extension='f2d')
        partial.pdf_mode = 'partial'
        with patch.object(fusion, 'PDF_WAIT_SECONDS', 2), patch('fusion_bulk_export.fusion.time.sleep'):
            _, counts = self.run_export(fake.data([partial, whole]))
        entries = {e['id']: e for e in self.manifest['entries']}
        self.assertEqual(entries['half']['status'], 'skipped')
        self.assertIn('incomplete drawing PDF', entries['half']['error'])
        self.assertEqual(entries['whole']['status'], 'done')  # the next drawing is still tried
        self.assertEqual(list((self.root / 'Hub' / 'Project').glob('Half*')), [])

    def test_a_design_whose_products_are_not_ready_at_once_exports_first_time(self):
        from unittest.mock import patch
        part = fake.File('Part', 'part')
        part.products_flaky = 2
        with patch('fusion_bulk_export.fusion.time.sleep'):
            self.run_export(fake.data([part]))
        entry = self.manifest['entries'][0]
        self.assertEqual((entry['status'], entry['attempts'], part.calls), ('done', 1, 1))


class FolderLockTests(Base):
    def lock_file(self, **facts):
        import os
        import socket
        import time
        value = dict(pid=os.getppid(), host=socket.gethostname(), started=time.time())
        value.update(facts)
        (self.root / 'export.lock').write_text(json.dumps(value))

    def test_a_folder_in_use_by_another_process_is_refused_untouched(self):
        from fusion_bulk_export.lock import FolderBusy
        from fusion_bulk_export import errors
        self.lock_file()  # the parent process: alive
        staged = self.root / '.staging' / 'theirs.f3d'
        staged.parent.mkdir()
        staged.write_bytes(b'being written')
        part = fake.File('Part', 'part')
        with self.assertRaises(FolderBusy) as caught:
            Exporter(self.root, self.adapter).run(fake.data([part]))
        self.assertTrue(staged.exists())
        self.assertEqual(part.calls, 0)
        self.assertFalse((self.root / 'manifest.json').exists())
        self.assertTrue((self.root / 'export.lock').exists())
        self.assertIn('Another export is using this folder', errors.friendly(caught.exception).text())

    def test_stale_locks_are_taken_over_and_the_lock_is_released(self):
        import os
        import subprocess
        import sys
        import time
        gone = subprocess.Popen([sys.executable, '-c', 'pass'])
        gone.wait()
        for facts in (dict(pid=gone.pid), dict(started=time.time() - 13 * 3600), dict(pid=os.getpid()), None):
            if facts is None:
                (self.root / 'export.lock').write_text('{half')
            else:
                self.lock_file(**facts)
            self.run_export(fake.data([fake.File('Part', 'part')]))
            self.assertFalse((self.root / 'export.lock').exists())

    def test_the_lock_is_released_when_the_run_fails(self):
        from unittest.mock import patch
        with patch.object(Exporter, '_run', side_effect=RuntimeError('Synthetic')):
            with self.assertRaises(RuntimeError):
                Exporter(self.root, self.adapter).run(fake.data([]))
        self.assertFalse((self.root / 'export.lock').exists())


class WindowsPathTests(Base):
    def export_on(self, platform, data):
        exporter = Exporter(self.root, self.adapter)
        exporter.platform = platform
        exporter.run(data)
        self.manifest = json.loads((self.root / 'manifest.json').read_text())
        return exporter

    def test_names_are_capped_on_windows_and_long_paths_are_reported(self):
        from fusion_bulk_export import report
        deep = [fake.folder('x' * 100, files=[fake.File('y' * 100, 'deep')])]
        for _ in range(2):
            deep = [fake.folder('z' * 100, folders=deep)]
        data = fake.data([fake.File('Short', 'short')], deep)
        exporter = self.export_on('win32', data)
        entries = {e['id']: e for e in self.manifest['entries']}
        parts = entries['deep']['files'][0]['path'].split('/')
        self.assertEqual([len(part) for part in parts[2:5]], [64, 64, 64])
        self.assertTrue(parts[5].startswith('y' * 64 + ' v3.'))
        self.assertTrue(entries['deep']['long_path'])
        self.assertNotIn('long_path', entries['short'])
        text = report.summary_text(exporter.run_info['counts'], exporter.run_info, self.manifest, str(self.root))
        self.assertIn('1 item(s) have a path over 260 characters', text)
        self.assertIn('path over 260 characters', report.render_html(self.manifest, exporter.run_info, None))

    def test_other_systems_keep_longer_names_and_no_flag(self):
        deep = [fake.folder('x' * 100, files=[fake.File('y' * 100, 'deep')])]
        self.export_on('darwin', fake.data(folders=deep))
        entry = self.manifest['entries'][0]
        self.assertEqual(len(entry['files'][0]['path'].split('/')[2]), 100)
        self.assertNotIn('long_path', entry)


if __name__ == '__main__':
    unittest.main()
