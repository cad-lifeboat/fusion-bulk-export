"""Assemblies of linked designs: the design as it is, then a copy with its linked parts included
(links removed in memory only), and every rule that keeps the Fusion account untouched."""
import csv
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace as NS

from tests import fake_adsk as fake
from fusion_bulk_export import checks, core, report
from fusion_bulk_export.core import Exporter

fake.install()
from fusion_bulk_export import fusion  # noqa: E402
from fusion_bulk_export.fusion import FusionAdapter  # noqa: E402

LINKED = ' (with linked parts).f3d'


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.app = NS(documents=fake.Documents())
        self.adapter = FusionAdapter(self.app)
        self.adapter.clock = fake.clock
        for kept in (fusion.LEFT_OPEN, fusion.BROKEN):
            kept.clear()
            self.addCleanup(kept.clear)

    def run_export(self, data, **kwargs):
        exporter = Exporter(self.root, self.adapter)
        exporter.clock = fake.clock
        self.counts = exporter.run(data, **kwargs)
        self.manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        self.exporter = exporter
        return exporter

    def entry(self, identity='assembly'):
        return next(e for e in self.manifest['entries'] if e['id'] == identity)

    def paths(self, identity='assembly'):
        return {info['role']: self.root / info['path'] for info in self.entry(identity)['files']}

    def assembly(self, **shape):
        return fake.linked_assembly(fake.File('Assembly', 'assembly', references=2), **shape)


class LinkedCopyTests(Base):
    def test_original_first_then_the_copy_with_linked_parts(self):
        assembly = self.assembly(parts=3)
        self.run_export(fake.data([assembly]))
        entry = self.entry()
        self.assertEqual([info['role'] for info in entry['files']], ['archive', 'step', 'archive_with_linked_parts'])
        files = self.paths()
        self.assertEqual(files['archive'].name, 'Assembly v3.f3d')
        self.assertEqual(files['archive_with_linked_parts'].name, 'Assembly v3' + LINKED)
        # The design's own archive was written while its links were all there; the copy after
        # every one was removed.
        self.assertEqual(fake.archive_links(files['archive']), 3)
        self.assertEqual(fake.archive_links(files['archive_with_linked_parts']), 0)
        self.assertEqual(entry['linked_occurrences'], 3)
        self.assertEqual({k: entry['linked_copy'][k] for k in ('status', 'reason', 'passes', 'broken')},
                         dict(status='saved', reason=None, passes=1, broken=3))
        self.assertEqual(entry['thumbnail']['role'], 'thumbnail')
        # Modified in memory, then closed without saving; nothing is left open.
        document = assembly.documents[0]
        self.assertTrue(document.isModified)
        self.assertEqual(document.closes, [False])
        self.assertEqual(self.app.documents.items, [])
        self.assertEqual(fusion.BROKEN, set())
        self.assertEqual(self.counts['linked_copies'], 1)
        self.assertNotIn('account_changed', entry)

    def test_nested_links_are_removed_pass_after_pass(self):
        assembly = self.assembly(parts=2, nested=2)
        self.run_export(fake.data([assembly]))
        copy = self.entry()['linked_copy']
        self.assertEqual((copy['status'], copy['passes'], copy['broken']), ('saved', 2, 6))
        self.assertEqual(fake.archive_links(self.paths()['archive_with_linked_parts']), 0)

    def test_inner_links_wait_for_the_next_pass(self):
        # A linked component listed inside another (its assemblyContext is linked) is left for the
        # next pass, read afresh, instead of being broken through its parent's old state.
        part = fake.component('p')
        outer = fake.Occurrence(part, referenced=True)
        inner = fake.Occurrence(part, referenced=True, parent=outer)
        loose = fake.Occurrence(part, referenced=True, parent=fake.Occurrence(part))
        self.assertEqual(fusion.outermost([outer, inner, loose]), [outer, loose])
        gone = fake.Occurrence(part, referenced=True)
        gone.isValid = False
        self.assertEqual(fusion.outermost([gone]), [])

    def test_top_level_links_too(self):
        self.run_export(fake.data([self.assembly(parts=2, under_local=False)]))
        self.assertEqual(self.entry()['linked_copy']['status'], 'saved')

    def test_references_without_linked_components_give_no_copy(self):
        # Seen on real Fusion: a design with child references but no occurrences at all.
        assembly = fake.File('Assembly', 'assembly', references=2)
        assembly.root = fake.component('root', [fake.body()])
        self.run_export(fake.data([assembly]))
        entry = self.entry()
        self.assertEqual([info['role'] for info in entry['files']], ['archive', 'step'])
        self.assertEqual(entry['linked_occurrences'], 0)
        self.assertNotIn('linked_copy', entry)
        self.assertFalse(assembly.documents[0].isModified)
        self.assertEqual(self.counts['linked_copies'], 0)

    def test_a_plain_part_is_untouched(self):
        part = fake.File('Part', 'part')
        self.run_export(fake.data([part]))
        self.assertEqual(self.entry('part')['linked_occurrences'], 0)
        self.assertFalse(part.documents[0].isModified)

    def test_option_off_removes_no_link(self):
        assembly = self.assembly(parts=2)
        self.run_export(fake.data([assembly]), linked_copies=False)
        entry = self.entry()
        self.assertEqual(len(entry['files']), 2)
        self.assertEqual(entry['linked_copy'], dict(status='off'))
        self.assertFalse(assembly.documents[0].isModified)
        self.assertFalse(self.exporter.run_info['linked_copies'])
        # Not re-exported just to add the copy when the option is on again; the report says how.
        assembly.calls = 0
        self.run_export(fake.data([assembly]))
        self.assertEqual((assembly.calls, self.counts['up_to_date']), (0, 1))
        page = report.render_html(self.manifest, self.exporter.run_info, self.counts)
        self.assertIn('1 assembly was exported with &quot;Include linked parts&quot; off; export into a new folder '
                      'to get its copy with linked parts.', page)


class OpenInFusionTests(Base):
    def test_a_design_the_user_has_open_keeps_its_links(self):
        assembly = self.assembly(parts=2)
        mine = fake.Document(assembly, self.app.documents)  # shown in Fusion: the user's
        self.app.documents.items.append(mine)
        self.run_export(fake.data([assembly]))
        entry = self.entry()
        self.assertEqual(entry['status'], 'done')
        self.assertEqual(len(entry['files']), 2)
        self.assertEqual(entry['linked_copy']['reason'], 'open in Fusion; close it to get the copy with linked parts')
        self.assertFalse(mine.isModified)
        self.assertFalse(mine.closed)
        self.assertTrue(all(o.breaks == 0 for o in mine.root.allOccurrences.items))
        page = report.render_html(self.manifest, self.exporter.run_info, self.counts)
        self.assertIn('no copy with linked parts: open in Fusion; close it to get the copy with linked parts.', page)
        # Once the user has closed it, the next run exports it again, now with its copy.
        self.app.documents.items.remove(mine)
        self.run_export(fake.data([assembly]))
        self.assertEqual(self.entry()['linked_copy']['status'], 'saved')
        self.assertEqual(self.counts['linked_copies'], 1)

    def test_a_document_that_fusion_hands_back_open_already_keeps_its_links(self):
        # Opened hidden by something else (not this add-in): not this export's to change.
        assembly = self.assembly(parts=1)
        hidden = fake.Document(assembly, self.app.documents, visible=False)
        self.app.documents.items.append(hidden)
        self.run_export(fake.data([assembly]))
        self.assertEqual(self.entry()['linked_copy']['retry'], True)
        self.assertFalse(hidden.isModified)


class FailureTests(Base):
    def assert_original_stands(self, assembly, reason):
        entry = self.entry()
        self.assertEqual(entry['status'], 'done')
        self.assertEqual([info['role'] for info in entry['files']], ['archive', 'step'])
        self.assertIn(reason, entry['linked_copy']['reason'])
        self.assertFalse(any(name.endswith(LINKED) for _, _, names in os.walk(str(self.root)) for name in names))
        self.assertEqual(assembly.documents[0].closes, [False])
        self.assertEqual(self.app.documents.items, [])

    def test_break_link_answering_false(self):
        assembly = self.assembly(parts=2, break_result=False)
        self.run_export(fake.data([assembly]))
        self.assert_original_stands(assembly, 'breakLink returned false')

    def test_break_link_raising(self):
        assembly = self.assembly(parts=2, break_result='3 : the reference is read-only')
        self.run_export(fake.data([assembly]))
        self.assert_original_stands(assembly, 'Fusion could not remove a link: 3 : the reference is read-only')

    def test_links_that_never_run_out_stop_after_ten_passes(self):
        assembly = self.assembly(parts=1, endless=True)
        self.run_export(fake.data([assembly]))
        self.assert_original_stands(assembly, 'still there after 10 passes')
        self.assertEqual(self.entry()['linked_copy']['passes'], 10)

    def test_doevents_between_passes(self):
        import adsk
        calls = []
        with patch.object(adsk, 'doEvents', lambda: calls.append(1)):
            self.run_export(fake.data([self.assembly(parts=1, nested=1)]))
        self.assertGreaterEqual(len(calls), 2)

    def test_the_copys_export_failing(self):
        assembly = self.assembly(parts=2)
        assembly.linked_error = RuntimeError('Synthetic export failure')
        self.run_export(fake.data([assembly]))
        self.assert_original_stands(assembly, 'Fusion could not write the copy with linked parts')

    def test_the_copys_export_returning_false(self):
        assembly = self.assembly(parts=2)
        original = fake.Manager.execute

        def execute(manager, path):
            if manager.document is not None and manager.document.links_removed:
                return False
            return original(manager, path)
        with patch.object(fake.Manager, 'execute', execute):
            self.run_export(fake.data([assembly]))
        self.assert_original_stands(assembly, 'returned false')

    def test_a_damaged_copy_is_removed_and_the_original_stands(self):
        assembly = self.assembly(parts=1)
        original = fake.Manager.execute

        def execute(manager, path):
            if manager.document is not None and manager.document.links_removed:
                Path(path).write_bytes(b'not a zip')
                return True
            return original(manager, path)
        with patch.object(fake.Manager, 'execute', execute):
            self.run_export(fake.data([assembly]))
        self.assert_original_stands(assembly, 'damaged')

    def test_a_full_disk_while_writing_the_copy_stops_the_run_and_closes_the_document(self):
        assembly = self.assembly(parts=2)
        assembly.linked_error = OSError(28, 'No space left on device')
        self.run_export(fake.data([assembly, fake.File('Next', 'next')]))
        self.assertEqual(self.exporter.run_info['stopped_code'], 'disk_full')
        self.assertEqual(assembly.documents[0].closes, [False])
        self.assertEqual(self.app.documents.items, [])

    def test_slow_link_removal_is_a_slow_item(self):
        assembly = self.assembly(parts=2, seconds=70)
        self.run_export(fake.data([assembly]))
        entry = self.entry()
        self.assertTrue(entry['linked_copy']['slow'])
        self.assertEqual(entry['linked_copy']['seconds'], 140)
        self.assertGreaterEqual(entry['slow_seconds'], 140)
        page = report.render_html(self.manifest, self.exporter.run_info, self.counts)
        self.assertIn('2 min 20 s of it removing links for the copy with linked parts', page)

    def test_a_document_fusion_will_not_close_is_recorded_and_never_exported_from_again(self):
        assembly = self.assembly(parts=1)
        assembly.close_result = False
        self.run_export(fake.data([assembly]))
        entry = self.entry()
        self.assertTrue(entry['linked_copy']['close_failed'])
        self.assertEqual(entry['documents_left_open'], 1)
        document = assembly.documents[0]
        self.assertTrue(all(save is False for save in document.closes))
        self.assertIn('assembly', fusion.BROKEN)
        # The next run (a new version to export) meets that hidden, changed document: it is not
        # exported from; closing it is tried, and while Fusion refuses, the design waits.
        assembly.versionNumber = assembly.latestVersionNumber = 4
        self.run_export(fake.data([assembly]))
        entry = next(e for e in self.manifest['entries'] if e['version'] == 4)
        self.assertEqual(entry['status'], 'failed')
        self.assertIn('had its links removed', entry['error'])
        self.assertEqual(len(assembly.documents), 1)
        # Once Fusion closes it, the design is opened afresh and exported.
        assembly.close_result = True
        self.run_export(fake.data([assembly]))
        entry = next(e for e in self.manifest['entries'] if e['version'] == 4)
        self.assertEqual((entry['status'], entry['linked_copy']['status']), ('done', 'saved'))
        self.assertEqual(fusion.BROKEN, set())


class AccountGuardTests(Base):
    def test_a_changed_account_stops_the_run_at_once(self):
        assembly = self.assembly(parts=1)

        def saved_meanwhile(document):
            document.dataFile.latestVersionNumber += 1  # what a save would do
        assembly.on_close = saved_meanwhile
        later = fake.File('Later', 'later')
        asked = []
        exporter = self.run_export(fake.data([assembly, later]),
                                   on_trouble=lambda reason, error: asked.append(reason) or 'retry')
        self.assertTrue(self.entry()['account_changed'])
        self.assertEqual(exporter.run_info['stopped_code'], 'account_changed')
        self.assertEqual(asked, [])  # never "try again?": the run must not go on
        self.assertEqual(later.calls, 0)
        log = (self.root / 'export.log').read_text(encoding='utf-8')
        self.assertIn('ERROR account changed', log)
        data = report.summary(self.counts, exporter.run_info, self.manifest, self.root)
        self.assertTrue(data['headline'].startswith('Stopped: '))
        self.assertIn('Warnings', [label for label, _ in data['rows']])
        page = report.render_html(self.manifest, exporter.run_info, self.counts)
        self.assertIn('Check its version history in Fusion', page)

    def test_the_guard_reads_the_file_before_and_after(self):
        assembly = self.assembly(parts=1)
        reads = []
        original = core.cloud_state
        with patch.object(core, 'cloud_state', lambda file: reads.append(file.id) or original(file)):
            self.run_export(fake.data([assembly, fake.File('Part', 'part')]))
        self.assertEqual(reads.count('assembly'), 2)
        self.assertEqual(reads.count('part'), 1)  # no link removed: nothing to compare
        self.assertNotIn('account_changed', self.entry())

    def test_unreadable_version_facts_are_no_alarm(self):
        self.assertIsNone(core.cloud_state(NS()))

    def test_save_apis_fail_loudly_in_the_synthetic_fusion(self):
        document = fake.Document(fake.File())
        for call in (document.save, document.saveAs, document.upload):
            with self.assertRaises(fake.AccountWrite):
                call('x')


class ManifestTests(Base):
    def test_resume_checks_all_three_files(self):
        assembly = self.assembly(parts=1)
        self.run_export(fake.data([assembly]))
        assembly.calls = 0
        self.run_export(fake.data([assembly]))
        self.assertEqual((assembly.calls, self.counts['up_to_date']), (0, 1))
        self.paths()['archive_with_linked_parts'].unlink()
        self.run_export(fake.data([assembly]))
        self.assertEqual((assembly.calls, self.counts['exported'], self.counts['linked_copies']), (1, 1, 1))
        self.assertTrue(self.paths()['archive_with_linked_parts'].is_file())

    def test_an_assembly_exported_before_the_option_is_not_exported_again(self):
        assembly = self.assembly(parts=1)
        self.run_export(fake.data([assembly]), linked_copies=False)
        # As an older version wrote it: no roles, no linked facts.
        manifest = self.manifest
        entry = manifest['entries'][0]
        for key in ('linked_occurrences', 'linked_copy'):
            entry.pop(key)
        for info in entry['files']:
            info.pop('role')
        entry['files'].reverse()  # any order
        (self.root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        assembly.calls = 0
        self.run_export(fake.data([assembly]))
        self.assertEqual((assembly.calls, self.counts['up_to_date']), (0, 1))
        note = '1 assembly was exported before this option existed; export into a new folder to get its copy with linked parts.'
        self.assertIn(note, report.summary(self.counts, self.exporter.run_info, self.manifest, self.root)['notes'])
        self.assertIn(note, report.render_html(self.manifest, self.exporter.run_info, self.counts))

    def test_complete_entries_by_role_or_extension(self):
        design = dict(kind='design', files=[dict(path='a v1.step'), dict(path='a v1.f3d')])
        self.assertTrue(core.complete(design))
        self.assertTrue(core.complete(dict(kind='design', files=[
            dict(path='a v1.f3d', role='archive'), dict(path='a v1.step', role='step'),
            dict(path='a v1' + LINKED, role='archive_with_linked_parts')])))
        self.assertFalse(core.complete(dict(kind='design', files=[dict(path='a v1.f3d')])))
        self.assertFalse(core.complete(dict(kind='design', files=[dict(path='a.f3d'), dict(path='b.f3d'),
                                                                   dict(path='a.step')])))
        self.assertTrue(core.complete(dict(kind='file', files=[dict(path='x.pdf')])))

    def test_link_map_points_every_link_at_its_exported_file(self):
        # The assembly is listed (and exported) before the designs it links to.
        assembly = self.assembly(parts=1)
        children = [assembly.childReferences.item(i) for i in range(2)]
        self.run_export(fake.data([assembly, children[0]]))
        references = {r['id']: r for r in self.entry()['references']}
        self.assertEqual(references[children[0].id]['path'], self.entry(children[0].id)['files'][0]['path'])
        self.assertTrue((self.root / references[children[0].id]['path']).is_file())
        self.assertIsNone(references[children[1].id]['path'])  # not exported here

    def test_a_long_name_keeps_the_whole_suffix_on_windows(self):
        assembly = fake.linked_assembly(fake.File('L' * 90, 'assembly', references=1), parts=1)
        exporter = Exporter(self.root, self.adapter)
        exporter.platform = 'win32'
        exporter.run(fake.data([assembly]))
        self.manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        name = self.paths()['archive_with_linked_parts'].name
        self.assertEqual(name, 'L' * 64 + ' v3' + LINKED)

    def test_report_csv_and_check(self):
        self.run_export(fake.data([self.assembly(parts=1), fake.File('Part', 'part')]))
        page = report.render_html(self.manifest, self.exporter.run_info, self.counts)
        self.assertIn('>F3D with linked parts</a>', page)
        self.assertIn('>F3D</a>', page)
        self.assertIn('1 assembly also saved with its linked parts included', page)
        rows = {r['design']: r for r in csv.DictReader(io.StringIO(report.render_csv(self.manifest, None)))}
        self.assertTrue(rows['Assembly']['archive_with_linked_parts'].endswith('Assembly v3' + LINKED))
        self.assertTrue(rows['Assembly']['archive'].endswith('Assembly v3.f3d'))
        self.assertEqual(rows['Part']['archive_with_linked_parts'], '')
        data = report.summary(self.counts, self.exporter.run_info, self.manifest, self.root)
        self.assertIn('1 assembly also saved with its linked parts included.', data['notes'])
        lines = []
        totals = checks.check_folder(self.root, show=lambda n, e, line, p, notes: lines.append((line, p)))
        self.assertEqual((totals['failed_checks'], totals['linked_copies']), (0, 1))
        self.assertIn('and a copy with linked parts', ' '.join(line for line, _ in lines))
        self.paths()['archive_with_linked_parts'].write_bytes(b'damaged')
        totals = checks.check_folder(self.root)
        self.assertEqual(totals['failed_checks'], 1)

    def test_plural_texts(self):
        self.assertEqual(report.linked_copies_text(12), '12 assemblies also saved with their linked parts included')
        self.assertEqual(report.linked_copies_text(1), '1 assembly also saved with its linked parts included')
        self.assertEqual(report.older_assemblies_text(3), '3 assemblies were exported before this option existed; '
                         'export into a new folder to get their copies with linked parts.')


class SafetySourceTests(unittest.TestCase):
    """breakLink is called in one place only, and that place is guarded (fusion.break_links, called
    from FusionAdapter.linked_copy, which runs only on a hidden document this export opened)."""
    def test_break_link_is_called_only_by_break_links(self):
        import ast
        package = Path(fusion.__file__).resolve().parent
        found = []
        for path in sorted(package.glob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for function in ast.walk(tree):
                if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for node in ast.walk(function):
                        if isinstance(node, ast.Attribute) and node.attr == 'breakLink':
                            found.append((path.name, function.name))
        self.assertEqual(sorted(set(found)), [('fusion.py', 'break_links')])
        callers = []
        tree = ast.parse(Path(fusion.__file__).read_text(encoding='utf-8'))
        for function in ast.walk(tree):
            if isinstance(function, ast.FunctionDef):
                for node in ast.walk(function):
                    if isinstance(node, ast.Call) and getattr(node.func, 'id', None) == 'break_links':
                        callers.append(function.name)
        self.assertEqual(callers, ['linked_copy'])


if __name__ == '__main__':
    unittest.main()
