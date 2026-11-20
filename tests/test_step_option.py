"""The STEP file as an option (on by default): a run without it writes each design's .f3d only,
records step: "off", and every reader (resume, check, report, counts) treats the missing STEP as
intended. Resuming across the two modes adds STEP files but never deletes one."""
import csv
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest

from tests import fake_adsk as fake
from fusion_bulk_export import checks, core, report
from fusion_bulk_export.core import Exporter

fake.install()
from fusion_bulk_export.fusion import FusionAdapter  # noqa: E402


class StepOptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.adapter = FusionAdapter(NS(documents=fake.Documents()))
        self.files = [fake.File('Bracket', 'bracket'), fake.File('Plate', 'plate')]

    def run_export(self, **kwargs):
        self.exporter = Exporter(self.root, self.adapter)
        self.counts = self.exporter.run(fake.data(self.files), **kwargs)
        self.manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        return self.counts

    def steps(self):
        return sorted(path.name for path in self.root.rglob('*.step'))

    def test_without_step_only_the_archive_is_written_and_recorded(self):
        counts = self.run_export(step=False)
        self.assertEqual((counts['exported'], counts['failed']), (2, 0))
        self.assertEqual(self.steps(), [])
        for entry in self.manifest['entries']:
            self.assertEqual((entry['status'], entry['step']), ('done', 'off'))
            self.assertEqual([info['role'] for info in entry['files']], ['archive'])
            self.assertEqual(entry['warnings'], [])
        self.assertIs(self.exporter.run_info['step'], False)
        self.assertEqual(report.warned(self.manifest), 0)
        # Resumed without STEP: up to date, nothing exported again.
        counts = self.run_export(step=False)
        self.assertEqual((counts['exported'], counts['up_to_date']), (0, 2))
        self.assertEqual([file.calls for file in self.files], [1, 1])

    def test_a_folder_without_step_gets_every_step_when_run_with_it(self):
        self.run_export(step=False)
        counts = self.run_export()
        self.assertEqual((counts['exported'], counts['up_to_date']), (2, 0))
        self.assertEqual(self.steps(), ['Bracket v3.step', 'Plate v3.step'])
        for entry in self.manifest['entries']:
            self.assertNotIn('step', entry)
            self.assertEqual(sorted(info['role'] for info in entry['files']), ['archive', 'step'])
        self.assertEqual(len(self.manifest['entries']), 2)

    def test_a_folder_with_step_run_without_it_exports_and_deletes_nothing(self):
        self.run_export()
        before = self.steps()
        self.assertEqual(len(before), 2)
        counts = self.run_export(step=False)
        self.assertEqual((counts['exported'], counts['up_to_date']), (0, 2))
        self.assertEqual([file.calls for file in self.files], [1, 1])
        self.assertEqual(self.steps(), before)
        for entry in self.manifest['entries']:
            self.assertNotIn('step', entry)
            self.assertEqual(len(entry['files']), 2)

    def test_check_report_and_table_treat_it_as_intended(self):
        self.run_export(step=False)
        lines = []
        totals = checks.check_folder(self.root, show=lambda n, e, line, p, notes: lines.append((line, p)))
        self.assertEqual((totals['failed_checks'], totals['step_off'], totals['done']), (0, 2, 2))
        self.assertTrue(all(line.endswith('STEP not requested') and not problems for line, problems in lines))
        page = report.render_html(self.manifest, self.exporter.run_info, self.counts)
        self.assertEqual(page.count('<span class="muted">STEP not requested</span>'), 2)
        self.assertIn('Note: STEP files were not requested for this export.', page)
        rows = list(csv.DictReader(io.StringIO(report.render_csv(self.manifest, self.exporter.run_info))))
        self.assertEqual([row['step'] for row in rows], ['STEP not requested'] * 2)
        self.assertTrue(all(row['archive'].endswith('.f3d') and row['status'] == 'done' for row in rows))
        data = report.summary(self.counts, self.exporter.run_info, self.manifest, self.root)
        self.assertEqual(data['headline'], 'Finished')
        self.assertIn(core.STEP_OFF_NOTE, data['notes'])
        self.assertNotIn('Warnings', [label for label, _ in data['rows']])
        # A run with STEP says nothing of it.
        self.run_export()
        data = report.summary(self.counts, self.exporter.run_info, self.manifest, self.root)
        self.assertNotIn(core.STEP_OFF_NOTE, data['notes'])
        self.assertNotIn('STEP not requested', report.render_html(self.manifest, self.exporter.run_info, self.counts))

    def test_a_step_that_failed_is_a_warning_in_check_not_a_problem(self):
        original = fake.Manager.execute
        fake.Manager.execute = lambda manager, path: False if path.endswith('.step') else original(manager, path)
        try:
            self.run_export()
        finally:
            fake.Manager.execute = original
        lines = []
        totals = checks.check_folder(self.root, show=lambda n, e, line, p, notes: lines.append((line, p)))
        self.assertEqual((totals['failed_checks'], totals['step_failed'], totals['warnings']), (0, 2, 2))
        self.assertTrue(all('STEP not written' in line for line, _ in lines))
        page = report.render_html(self.manifest, self.exporter.run_info, self.counts)
        self.assertEqual(page.count('Warning: Saved, but Fusion could not write its STEP file'), 2)
        self.assertEqual(page.count(report.STEP_RETRIED), 2)
        # Resumed without STEP: the .f3d is all that is wanted, so it is up to date.
        counts = self.run_export(step=False)
        self.assertEqual(counts['up_to_date'], 2)

    def test_complete_by_intent(self):
        archive = dict(path='a v1.f3d', role='archive')
        step = dict(path='a v1.step', role='step')
        self.assertTrue(core.complete(dict(kind='design', step='off', files=[archive])))
        self.assertTrue(core.complete(dict(kind='design', step='failed', files=[archive])))
        self.assertFalse(core.complete(dict(kind='design', files=[archive])))
        self.assertFalse(core.complete(dict(kind='design', step='off', files=[archive, step])))
        self.assertTrue(core.complete(dict(kind='design', files=[archive, step])))


if __name__ == '__main__':
    unittest.main()
