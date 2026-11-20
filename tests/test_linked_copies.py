"""Assemblies of linked designs. By default only the design as it is (no link is ever removed).
With the testing-only switch (core.LINKED_COPIES_ENV) also an experimental copy with its linked
parts included (links removed in memory only, by one of the recipes), and every rule that keeps
the Fusion account untouched."""
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

LINKED = ' (with linked parts, experimental).f3d'


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.app = NS(documents=fake.Documents())
        self.adapter = FusionAdapter(self.app)
        self.adapter.clock = fake.clock
        for kept in (fusion.LEFT_OPEN, fusion.BROKEN, fake.CALLS):
            kept.clear()
            self.addCleanup(kept.clear)
        fake.BREAK_LINK_CALLS[0] = 0
        environment = patch.dict(os.environ, {})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(core.LINKED_COPIES_ENV, None)

    def run_export(self, data, **kwargs):
        """An export with the experiment on (the default recipe) unless linked_copies is given."""
        kwargs.setdefault('linked_copies', '1')
        exporter = Exporter(self.root, self.adapter)
        exporter.clock = fake.clock
        self.counts = exporter.run(data, **kwargs)
        self.manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        self.exporter = exporter
        return exporter

    def default_run(self, data, **kwargs):
        """An export as users get it: the switch not set, nothing passed."""
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
        self.assertEqual({k: entry['linked_copy'][k] for k in ('status', 'reason', 'passes', 'broken', 'recipe',
                                                               'computed')},
                         dict(status='saved', reason=None, passes=1, broken=3, recipe='both', computed=True))
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

    def test_off_by_default_no_link_is_removed(self):
        assembly = self.assembly(parts=2, nested=1)
        self.default_run(fake.data([assembly, fake.File('Part', 'part')]))
        entry = self.entry()
        self.assertEqual(fake.BREAK_LINK_CALLS[0], 0)
        self.assertEqual(fake.CALLS, [])
        self.assertEqual([info['role'] for info in entry['files']], ['archive', 'step'])
        self.assertEqual(entry['linked_occurrences'], 2)
        self.assertNotIn('linked_copy', entry)
        self.assertNotIn('links_broken', entry)
        self.assertFalse(assembly.documents[0].isModified)
        self.assertEqual(assembly.documents[0].closes, [False])
        self.assertEqual(self.counts['linked_copies'], 0)
        self.assertNotIn('experiment_linked_copies', self.exporter.run_info)
        self.assertFalse(any(name.endswith('.f3d') and 'linked parts' in name
                             for _, _, names in os.walk(str(self.root)) for name in names))
        # Nothing about copies with linked parts anywhere the user reads.
        page = report.render_html(self.manifest, self.exporter.run_info, self.counts)
        data = report.summary(self.counts, self.exporter.run_info, self.manifest, self.root)
        table = report.render_csv(self.manifest, self.exporter.run_info)
        for text in (page, json.dumps(data), table):
            self.assertNotIn('linked parts', text)
            self.assertNotIn('experiment', text)

    def test_off_also_for_values_that_are_not_a_recipe(self):
        for value in ('0', 'off', 'yes', '', 'true'):
            with patch.dict(os.environ, {core.LINKED_COPIES_ENV: value}):
                self.default_run(fake.data([self.assembly(parts=1)]))
            self.assertEqual(fake.BREAK_LINK_CALLS[0], 0, value)
        self.default_run(fake.data([self.assembly(parts=1)]), linked_copies=False)
        self.assertEqual(fake.BREAK_LINK_CALLS[0], 0)

    def test_the_switch_in_the_environment_turns_it_on(self):
        with patch.dict(os.environ, {core.LINKED_COPIES_ENV: 'Innermost'}):
            self.default_run(fake.data([self.assembly(parts=1)]))
        self.assertEqual(self.entry()['linked_copy']['recipe'], 'innermost')
        self.assertEqual(self.exporter.run_info['experiment_linked_copies'], 'innermost')
        self.assertIn('experiment: copies with linked parts on, recipe innermost',
                      (self.root / 'export.log').read_text(encoding='utf-8'))

    def test_recipes(self):
        self.assertEqual([core.link_recipe(v) for v in ('1', ' innermost', 'COMPUTE', 'both', '0', None, 'x', True)],
                         ['both', 'innermost', 'compute', 'both', None, None, None, 'both'])
        self.assertIsNone(core.linked_copies_experiment({}))
        self.assertEqual(core.linked_copies_experiment({core.LINKED_COPIES_ENV: '1'}), 'both')

    def test_an_assembly_exported_with_it_off_is_not_exported_again_when_it_is_on(self):
        assembly = self.assembly(parts=2)
        self.default_run(fake.data([assembly]))
        assembly.calls = 0
        self.run_export(fake.data([assembly]))
        self.assertEqual((assembly.calls, self.counts['up_to_date']), (0, 1))
        self.assertEqual(fake.BREAK_LINK_CALLS[0], 0)


class RecipeTests(Base):
    """The call order of each recipe, as probed on real Fusion: innermost first with doEvents after
    each breakLink; outermost first, then computeAll and 5 doEvents; the default (1) both."""
    def deep(self, compute=None):
        # top: a linked component at the top; outer: one below a local sub-component; inner: one
        # inside outer (depth 2). All are listed at once.
        assembly = fake.File('Assembly', 'assembly', references=2)
        assembly.compute = compute

        def make():
            local = fake.Occurrence(fake.component('local-sub', [fake.body()]))
            top = fake.Occurrence(fake.component('top', [fake.body()]), referenced=True)
            outer = fake.Occurrence(fake.component('outer', [fake.body()]), referenced=True, parent=local)
            inner = fake.Occurrence(fake.component('inner', [fake.body()]), referenced=True, parent=outer)
            return fake.component('root', [fake.body()], [local, top, outer, inner])
        assembly.make_root = make
        return assembly

    def calls(self, recipe, assembly=None):
        """breakLink, doEvents and computeAll calls from the first breakLink until the copy is exported."""
        import adsk
        original = fake.Manager.execute

        def execute(manager, path):
            if manager.document is not None and manager.document.links_removed:
                fake.CALLS.append(('export copy',))
            return original(manager, path)
        with patch.object(adsk, 'doEvents', lambda: fake.CALLS.append(('doEvents',))), \
                patch.object(fake.Manager, 'execute', execute):
            self.run_export(fake.data([assembly or self.deep()]), linked_copies=recipe)
        names = [call[0] if call[0] != 'breakLink' else call[1] for call in fake.CALLS]
        first = next(i for i, call in enumerate(fake.CALLS) if call[0] == 'breakLink')
        return names[first:names.index('export copy')]

    def test_innermost_first_with_doevents_after_each_break(self):
        self.assertEqual(self.calls('innermost'), ['inner', 'doEvents', 'outer', 'doEvents', 'top', 'doEvents'])
        self.assertEqual(self.entry()['linked_copy']['status'], 'saved')
        self.assertNotIn('computed', self.entry()['linked_copy'])

    def test_outermost_first_then_compute_all_and_five_doevents(self):
        self.assertEqual(self.calls('compute'), ['top', 'outer', 'doEvents', 'inner', 'doEvents', 'computeAll']
                         + ['doEvents'] * 5)
        self.assertEqual(self.entry()['linked_copy']['computed'], True)

    def test_the_default_recipe_is_both(self):
        self.assertEqual(self.calls('1'), ['inner', 'doEvents', 'outer', 'doEvents', 'top', 'doEvents', 'computeAll']
                         + ['doEvents'] * 5)
        copy = self.entry()['linked_copy']
        self.assertEqual((copy['recipe'], copy['broken'], copy['passes']), ('both', 3, 1))
        self.assertEqual(fake.archive_links(self.paths()['archive_with_linked_parts']), 0)

    def test_a_fusion_without_compute_all_still_settles(self):
        self.assertEqual(self.calls('compute', self.deep(compute='missing'))[-5:], ['doEvents'] * 5)
        copy = self.entry()['linked_copy']
        self.assertEqual((copy['status'], copy['computed']), ('saved', 'this Fusion has no design.computeAll'))

    def test_compute_all_failing_is_recorded_and_the_copy_still_written(self):
        names = self.calls('both', self.deep(compute='3 : compute failed'))
        self.assertEqual(names[-6:], ['computeAll'] + ['doEvents'] * 5)
        copy = self.entry()['linked_copy']
        self.assertEqual((copy['status'], copy['computed']), ('saved', 'design.computeAll failed: 3 : compute failed'))

    def test_innermost_order(self):
        part = fake.component('p')
        local = fake.Occurrence(part)
        a = fake.Occurrence(part, referenced=True)
        b = fake.Occurrence(part, referenced=True, parent=local)
        c = fake.Occurrence(part, referenced=True, parent=b)
        gone = fake.Occurrence(part, referenced=True, parent=c)
        gone.isValid = False
        self.assertEqual(fusion.innermost([a, b, c, gone]), [c, b, a])


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

    def test_an_entry_written_before_roles_existed_is_still_up_to_date(self):
        assembly = self.assembly(parts=1)
        self.default_run(fake.data([assembly]))
        # As an older version wrote it: no roles, no linked facts.
        manifest = self.manifest
        entry = manifest['entries'][0]
        entry.pop('linked_occurrences')
        for info in entry['files']:
            info.pop('role')
        entry['files'].reverse()  # any order
        (self.root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        assembly.calls = 0
        self.default_run(fake.data([assembly]))
        self.assertEqual((assembly.calls, self.counts['up_to_date']), (0, 1))
        self.assertFalse(any('linked parts' in note
                             for note in report.summary(self.counts, self.exporter.run_info, self.manifest,
                                                        self.root)['notes']))

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
        # In a default run. The assembly is listed (and exported) before the designs it links to.
        assembly = self.assembly(parts=1)
        children = [assembly.childReferences.item(i) for i in range(2)]
        self.default_run(fake.data([assembly, children[0]]))
        references = {r['id']: r for r in self.entry()['references']}
        self.assertEqual(references[children[0].id]['path'], self.entry(children[0].id)['files'][0]['path'])
        self.assertTrue((self.root / references[children[0].id]['path']).is_file())
        self.assertIsNone(references[children[1].id]['path'])  # not exported here

    def test_a_long_name_keeps_the_whole_suffix_on_windows(self):
        assembly = fake.linked_assembly(fake.File('L' * 90, 'assembly', references=1), parts=1)
        exporter = Exporter(self.root, self.adapter)
        exporter.platform = 'win32'
        exporter.run(fake.data([assembly]), linked_copies='1')
        self.manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        name = self.paths()['archive_with_linked_parts'].name
        self.assertEqual(name, 'L' * 64 + ' v3' + LINKED)

    def test_report_csv_and_check(self):
        self.run_export(fake.data([self.assembly(parts=1), fake.File('Part', 'part')]))
        page = report.render_html(self.manifest, self.exporter.run_info, self.counts)
        self.assertIn('>F3D with linked parts (experimental)</a>', page)
        self.assertIn('>F3D</a>', page)
        rows = {r['design']: r for r in csv.DictReader(io.StringIO(report.render_csv(self.manifest, None)))}
        self.assertNotIn('archive_with_linked_parts', rows['Assembly'])
        self.assertTrue(rows['Assembly']['archive'].endswith('Assembly v3.f3d'))
        self.assertTrue(rows['Assembly']['step'].endswith('Assembly v3.step'))
        lines = []
        totals = checks.check_folder(self.root, show=lambda n, e, line, p, notes: lines.append((line, p)))
        self.assertEqual((totals['failed_checks'], totals['linked_copies']), (0, 1))
        self.assertIn('and an experimental copy with linked parts', ' '.join(line for line, _ in lines))
        self.paths()['archive_with_linked_parts'].write_bytes(b'damaged')
        totals = checks.check_folder(self.root)
        self.assertEqual(totals['failed_checks'], 1)


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
