"""Hidden bodies and designs without solid bodies in the STEP: hidden bodies are included when
Fusion's STEP options offer it (only the options change, never the design); when they cannot be,
the STEP leaving them out is a plain warning; a design with no solid bodies at all gets an empty
STEP with a quiet note, not a warning. Older folders read the same way."""
import contextlib
import csv
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest

from tests import fake_adsk as fake
from fusion_bulk_export import checks, report
from fusion_bulk_export.core import Exporter

fake.install()
from fusion_bulk_export.fusion import FusionAdapter  # noqa: E402

spec = importlib.util.spec_from_file_location('autorun_hidden', Path(__file__).parent.parent / 'scripts' / 'autorun.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

SKETCHES = 'No solid bodies (sketches only): its STEP is empty.'


class VerdictTests(unittest.TestCase):
    """step_verdict() on the counts seen in a real 491-design run (counts only)."""
    def verdict(self, solids, **facts):
        return checks.step_verdict(solids, facts)

    def test_no_solid_bodies_is_a_note_not_a_warning(self):
        result = self.verdict(0, solid_bodies=0, visible_solid_bodies=0, visible_solid_bodies_distinct=0)
        self.assertEqual((result['warnings'], result['note']),
                         ([], 'No solid bodies (sketches or surfaces only): its STEP is empty.'))
        self.assertEqual(self.verdict(0, solid_bodies=0, surface_bodies=0, mesh_bodies=0)['note'], SKETCHES)
        self.assertEqual(self.verdict(0, solid_bodies=0, surface_bodies=2, mesh_bodies=0)['note'],
                         'No solid bodies (surfaces only): its STEP has no solids.')
        self.assertEqual(self.verdict(0, solid_bodies=0, surface_bodies=1, mesh_bodies=3)['note'],
                         'No solid bodies (surfaces and meshes only): its STEP has no solids.')
        self.assertEqual(self.verdict(0, solid_bodies=0, step_includes_hidden=True)['warnings'], [])

    def test_all_bodies_hidden_and_not_included(self):
        for total, text in ((1, 'Its one body is hidden, so its STEP is empty; the body is in the .f3d.'),
                            (64, 'All its 64 bodies are hidden, so its STEP is empty; they are in the .f3d.')):
            for included in (False, None):
                result = self.verdict(0, solid_bodies=total, visible_solid_bodies=0, visible_solid_bodies_distinct=0,
                                      step_includes_hidden=included)
                self.assertEqual((result['warnings'], result['note'], result['hidden_left_out']), ([text], None, total))

    def test_some_hidden_left_out_plus_a_shortfall(self):
        result = self.verdict(5, solid_bodies=96, visible_solid_bodies=32, visible_solid_bodies_distinct=32)
        self.assertEqual(result['warnings'], ['Its STEP leaves out 64 hidden bodies; they are in the .f3d.',
                                              'STEP has 5 solids, fewer than the 32 distinct ones Fusion showed'])
        result = self.verdict(1, solid_bodies=7, visible_solid_bodies=3, visible_solid_bodies_distinct=3)
        self.assertEqual(result['warnings'], ['Its STEP leaves out 4 hidden bodies; they are in the .f3d.',
                                              'STEP has 1 solid, fewer than the 3 distinct ones Fusion showed'])
        result = self.verdict(3, solid_bodies=4, visible_solid_bodies=3, visible_solid_bodies_distinct=3)
        self.assertEqual(result['warnings'], ['Its STEP leaves out 1 hidden body; it is in the .f3d.'])

    def test_visible_bodies_and_an_empty_step_is_a_real_loss(self):
        result = self.verdict(0, solid_bodies=2, visible_solid_bodies=2, visible_solid_bodies_distinct=2)
        self.assertEqual(result['warnings'], ['STEP has no bodies (Fusion showed 2)'])

    def test_hidden_included_expects_every_solid_body(self):
        facts = dict(solid_bodies=96, visible_solid_bodies=32, visible_solid_bodies_distinct=32,
                     step_includes_hidden=True)
        self.assertEqual(self.verdict(96, **facts)['warnings'], [])
        self.assertEqual(self.verdict(200, **facts)['warnings'], [])  # more is only recorded
        self.assertEqual(self.verdict(40, **facts)['warnings'],
                         ['STEP has 40 solids, fewer than the 96 distinct ones in the design'])
        self.assertEqual(self.verdict(0, **facts)['warnings'], ['STEP has no bodies (96 solid bodies in the design)'])
        self.assertEqual(self.verdict(0, **facts)['hidden_left_out'], 0)

    def test_folders_from_older_versions_read_the_new_way(self):
        sketch = dict(status='done', solid_bodies=0, visible_solid_bodies=0, visible_solid_bodies_distinct=0,
                      step_solids=0, warnings=['STEP has no bodies'])
        hidden = dict(status='done', solid_bodies=3, visible_solid_bodies=0, visible_solid_bodies_distinct=0,
                      step_solids=0, warnings=['STEP has no bodies'])
        lost = dict(status='done', solid_bodies=2, visible_solid_bodies=2, visible_solid_bodies_distinct=2,
                    step_solids=0, warnings=['STEP has no bodies (Fusion showed 2)', 'something else'])
        self.assertEqual(checks.entry_step(sketch), dict(
            warnings=[], note='No solid bodies (sketches or surfaces only): its STEP is empty.', hidden_left_out=0))
        self.assertEqual(checks.entry_step(hidden)['warnings'],
                         ['All its 3 bodies are hidden, so its STEP is empty; they are in the .f3d.'])
        self.assertEqual(checks.entry_step(lost)['warnings'], ['STEP has no bodies (Fusion showed 2)', 'something else'])
        manifest = dict(entries=[sketch, hidden, lost])
        self.assertEqual((report.warned(manifest), report.hidden_left_out(manifest)), (2, 1))


class HiddenBodiesExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def run_export(self, files):
        exporter = Exporter(self.root, FusionAdapter(NS(documents=fake.Documents())))
        self.counts = exporter.run(fake.data(files))
        self.run_info = exporter.run_info
        self.manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        return {entry['id']: entry for entry in self.manifest['entries']}

    def summary(self):
        return report.summary(self.counts, self.run_info, self.manifest, self.root)

    def test_hidden_bodies_are_asked_for_and_the_design_is_left_alone(self):
        hidden = fake.body(shown=False)
        part = fake.component('part', [fake.body(), hidden])
        off = fake.component('off', [fake.body()])
        file = fake.File('Assembly', 'assembly')
        file.root = fake.component('root', [fake.body()], [fake.occurrence(part), fake.occurrence(off, visible=False)])
        file.step_solids, file.step_hidden_solids = 2, 2  # the hidden body and the hidden component's
        entry = self.run_export([file])['assembly']
        self.assertIs(entry['step_includes_hidden'], True)
        self.assertNotIn('step_hidden_error', entry)
        # root 1 + part 2 + the hidden component's 1: all four are expected, and the STEP has them.
        self.assertEqual((entry['solid_bodies'], entry['visible_solid_bodies'], entry['step_solids']), (4, 2, 4))
        self.assertEqual(entry['warnings'], [])
        # Only the export options changed: no body or occurrence was shown or hidden.
        self.assertFalse(hidden.isLightBulbOn)
        self.assertFalse(file.root.allOccurrences.item(1).isVisible)
        self.assertNotIn('Warnings', [label for label, _ in self.summary()['rows']])
        self.assertFalse(any('hidden' in note for note in self.summary()['notes']))

    def test_a_fusion_without_the_options_leaves_hidden_bodies_out_with_a_warning(self):
        some, every = fake.File('Some', 'some'), fake.File('Every', 'every')
        every.root = fake.component('root', [fake.body(shown=False), fake.body(shown=False)])
        every.step_solids = 0
        for file in (some, every):
            file.step_hidden_options = 'none'
        entries = self.run_export([some, every])
        self.assertIs(entries['some']['step_includes_hidden'], False)
        self.assertEqual(entries['some']['warnings'], ['Its STEP leaves out 1 hidden body; it is in the .f3d.'])
        self.assertEqual(entries['every']['warnings'],
                         ['All its 2 bodies are hidden, so its STEP is empty; they are in the .f3d.'])
        data = self.summary()
        self.assertIn(['Warnings', 2], data['rows'])
        note = ('2 designs have a STEP file without their hidden bodies: this version of Fusion cannot include them. '
                'The .f3d holds every body.')
        self.assertIn(note, data['notes'])
        self.assertIn(note, report.render_html(self.manifest, self.run_info, self.counts))
        status = '\n'.join(launcher.summary(self.root))
        self.assertIn('Warnings: 2 items exported with a warning', status)
        self.assertIn('Note: 2 designs have a STEP file without their hidden bodies', status)
        totals = checks.check_folder(self.root, strict=True)
        # Not a problem even when strict: the .f3d holds them.
        self.assertEqual((totals['hidden_left_out'], totals['failed_checks'], totals['warnings']), (2, 0, 2))

    def test_options_that_cannot_be_set_are_recorded_and_the_export_goes_on(self):
        refused, half = fake.File('Refused', 'refused'), fake.File('Half', 'half')
        refused.step_hidden_options, half.step_hidden_options = 'raise', 'bodies'
        entries = self.run_export([refused, half])
        self.assertEqual(self.counts['exported'], 2)
        self.assertIsNone(entries['refused']['step_includes_hidden'])
        self.assertIn('synthetic refusal', entries['refused']['step_hidden_error'])
        self.assertIsNone(entries['half']['step_includes_hidden'])
        self.assertEqual(entries['half']['step_hidden_error'], 'this Fusion has no isIncludingInvisibleComponents')
        # Not known to be included: compared with the visible bodies, the hidden one named.
        self.assertEqual(entries['refused']['warnings'], ['Its STEP leaves out 1 hidden body; it is in the .f3d.'])

    def test_a_design_with_no_solid_bodies_gets_a_note_not_a_warning(self):
        sketch, surfaces, meshes = fake.File('Sketch', 'sketch'), fake.File('Surfaces', 'surfaces'), fake.File('Mesh', 'mesh')
        sketch.root = fake.component('root')
        surfaces.root = fake.component('root', [fake.body(solid=False)])
        meshes.root = fake.component('root', [], meshes=2)
        for file in (sketch, surfaces, meshes):
            file.step_solids = 0
        entries = self.run_export([sketch, surfaces, meshes, fake.File('Part', 'part')])
        self.assertEqual(entries['sketch']['warnings'], [])
        self.assertEqual(entries['sketch']['step_note'], SKETCHES)
        self.assertEqual(entries['surfaces']['step_note'], 'No solid bodies (surfaces only): its STEP has no solids.')
        self.assertEqual(entries['mesh']['step_note'], 'No solid bodies (meshes only): its STEP has no solids.')
        self.assertEqual((entries['mesh']['surface_bodies'], entries['mesh']['mesh_bodies']), (0, 2))
        self.assertNotIn('mesh_bodies', entries['part'])  # counted only for a design with no solid bodies
        self.assertNotIn('Warnings', [label for label, _ in self.summary()['rows']])
        page = report.render_html(self.manifest, self.run_info, self.counts)
        self.assertIn('Note: ' + SKETCHES, page)
        self.assertNotIn('with a warning', page)
        rows = list(csv.DictReader(io.StringIO(report.render_csv(self.manifest, self.run_info))))
        self.assertIn(SKETCHES, [row['note'] for row in rows])
        self.assertNotIn('Warnings:', '\n'.join(launcher.summary(self.root)))
        shown = []
        totals = checks.check_folder(self.root, strict=True, show=lambda *args: shown.append(args))
        self.assertEqual((totals['no_solid_bodies'], totals['warnings'], totals['failed_checks']), (3, 0, 0))
        self.assertEqual([args for args in shown if args[3] or args[4]], [])  # check flags nothing

    def test_a_new_export_replaces_the_warning_an_older_version_stored(self):
        sketch = fake.File('Sketch', 'sketch')
        sketch.root = fake.component('root')
        sketch.step_solids = 0
        self.run_export([sketch])
        manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        entry = manifest['entries'][0]
        for key in ('step_note', 'step_includes_hidden'):
            entry.pop(key)
        entry['warnings'] = ['STEP has no bodies']  # as 0.3.2 recorded it
        (self.root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.assertEqual(launcher.main(['check', str(self.root)]), 0)
        self.assertNotIn('NOTE', stream.getvalue())
        self.assertNotIn('Warnings:', '\n'.join(launcher.summary(self.root)))


if __name__ == '__main__':
    unittest.main()
