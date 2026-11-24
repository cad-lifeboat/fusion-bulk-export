"""STEP only (a protocol job's formats ["step"], autorun.json's "archive": false): each design gets
its STEP and thumbnail but no .f3d, recorded honestly as archive: "off", and every reader (resume,
check, report, counts) treats the missing .f3d as intended. Nothing already exported is ever left
behind unrecorded, and a run that wants the .f3d adds it."""
import csv
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest

from tests import fake_adsk as fake

fake.install()
from fusion_bulk_export import autorun, checks, core, report  # noqa: E402
from fusion_bulk_export.core import Exporter  # noqa: E402
from fusion_bulk_export.fusion import FusionAdapter  # noqa: E402


class StepOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.adapter = FusionAdapter(NS(documents=fake.Documents()))
        self.files = [fake.File('Bracket', 'bracket'), fake.File('Plate', 'plate', references=1)]

    def run_export(self, **kwargs):
        self.exporter = Exporter(self.root, self.adapter)
        self.counts = self.exporter.run(fake.data(self.files), **kwargs)
        self.manifest = json.loads((self.root / 'manifest.json').read_bytes().decode('utf-8'))
        return self.counts

    def names(self, pattern):
        return sorted(path.name for path in self.root.rglob(pattern) if 'Older versions' not in path.parts)

    def test_only_the_step_is_written_and_recorded(self):
        counts = self.run_export(archive=False)
        self.assertEqual((counts['exported'], counts['failed']), (2, 0))
        self.assertEqual(self.names('*.f3d'), [])
        self.assertEqual(self.names('*.step'), ['Bracket v3.step', 'Plate v3.step'])
        for entry in self.manifest['entries']:
            self.assertEqual((entry['status'], entry['archive']), ('done', 'off'))
            self.assertNotIn('step', entry)
            self.assertEqual([info['role'] for info in entry['files']], ['step'])
            self.assertEqual(entry['thumbnail']['role'], 'thumbnail')
        self.assertIs(self.exporter.run_info['archive'], False)
        plate = next(e for e in self.manifest['entries'] if e['id'] == 'plate')
        self.assertEqual([link['path'] for link in plate['references']], [None])  # links name .f3d files
        # Resumed STEP-only: up to date.
        counts = self.run_export(archive=False)
        self.assertEqual((counts['exported'], counts['up_to_date']), (0, 2))
        self.assertEqual([file.calls for file in self.files], [1, 1])

    def test_a_step_only_folder_gets_its_f3d_when_run_with_it(self):
        self.run_export(archive=False)
        counts = self.run_export()
        self.assertEqual((counts['exported'], counts['up_to_date']), (2, 0))
        self.assertEqual(self.names('*.f3d'), ['Bracket v3.f3d', 'Plate v3.f3d'])
        for entry in self.manifest['entries']:
            self.assertNotIn('archive', entry)
            self.assertEqual(sorted(info['role'] for info in entry['files']), ['archive', 'step'])
        self.assertEqual(len(self.manifest['entries']), 2)

    def test_a_full_export_run_step_only_is_up_to_date_and_deletes_nothing(self):
        self.run_export()
        counts = self.run_export(archive=False)
        self.assertEqual((counts['exported'], counts['up_to_date']), (0, 2))
        self.assertEqual(self.names('*.f3d'), ['Bracket v3.f3d', 'Plate v3.f3d'])

    def test_an_f3d_only_entry_exported_again_step_only_keeps_its_f3d_recorded(self):
        self.run_export(step=False)
        counts = self.run_export(archive=False)  # its STEP is wanted now: exported again
        self.assertEqual(counts['exported'], 2)
        for entry in self.manifest['entries']:
            self.assertNotIn('archive', entry)  # the .f3d it had is written again, never left unrecorded
            self.assertEqual(sorted(info['role'] for info in entry['files']), ['archive', 'step'])
        self.assertEqual(checks.check_folder(self.root)['failed_checks'], 0)

    def test_a_step_fusion_will_not_write_fails_the_design(self):
        original = fake.Manager.execute
        fake.Manager.execute = lambda manager, path: False if path.endswith('.step') else original(manager, path)
        try:
            counts = self.run_export(archive=False)
        finally:
            fake.Manager.execute = original
        self.assertEqual((counts['exported'], counts['failed']), (0, 2))
        for entry in self.manifest['entries']:
            self.assertEqual((entry['status'], entry['files']), ('failed', []))
            self.assertIn('could not write the STEP file', entry['error'])
            self.assertEqual(entry['attempts'], 2)
        self.assertEqual(self.names('*.step') + self.names('*.f3d'), [])
        self.assertEqual(list((self.root / '.staging').glob('*')) if (self.root / '.staging').exists() else [], [])

    def test_check_report_and_table_say_f3d_not_requested(self):
        self.run_export(archive=False)
        lines = []
        totals = checks.check_folder(self.root, show=lambda n, e, line, p, notes: lines.append((line, p)))
        self.assertEqual((totals['failed_checks'], totals['archive_off'], totals['done'], totals['step_ok']),
                         (0, 2, 2, 2))
        self.assertTrue(all(line.startswith('STEP only (.f3d not requested)') and not problems
                            for line, problems in lines))
        page = report.render_html(self.manifest, self.exporter.run_info, self.counts)
        self.assertEqual(page.count('<span class="muted">.f3d not requested</span>'), 2)
        self.assertIn(core.ARCHIVE_OFF_NOTE, page)
        rows = list(csv.DictReader(io.StringIO(report.render_csv(self.manifest, self.exporter.run_info))))
        self.assertEqual([row['archive'] for row in rows], ['.f3d not requested'] * 2)
        self.assertTrue(all(row['step'].endswith('.step') for row in rows))
        data = report.summary(self.counts, self.exporter.run_info, self.manifest, self.root)
        self.assertIn(core.ARCHIVE_OFF_NOTE, data['notes'])

    def test_check_finds_a_missing_step_of_a_step_only_entry(self):
        self.run_export(archive=False)
        next(self.root.rglob('Bracket v3.step')).unlink()
        totals = checks.check_folder(self.root)
        self.assertEqual((totals['failed_checks'], totals['missing']), (1, 1))
        broken = dict(self.manifest['entries'][1], files=[])
        self.manifest['entries'][1] = broken
        (self.root / 'manifest.json').write_bytes(json.dumps(self.manifest).encode('utf-8'))
        problems = []
        checks.check_folder(self.root, show=lambda n, e, line, p, notes: problems.extend(p))
        self.assertIn('STEP not recorded', problems)

    def test_complete_by_intent(self):
        archive = dict(path='a v1.f3d', role='archive')
        step = dict(path='a v1.step', role='step')
        self.assertTrue(core.complete(dict(kind='design', archive='off', files=[step])))
        self.assertFalse(core.complete(dict(kind='design', archive='off', files=[archive, step])))
        self.assertFalse(core.complete(dict(kind='design', archive='off', files=[])))
        self.assertFalse(core.complete(dict(kind='design', archive='off', step='off', files=[])))

    def test_older_versions_of_a_step_only_design_are_moved(self):
        self.run_export(archive=False)
        for file in self.files:
            file.versionNumber = file.latestVersionNumber = 4
        self.run_export(archive=False)
        self.assertEqual(self.names('*.step'), ['Bracket v4.step', 'Plate v4.step'])
        moved = [e for e in self.manifest['entries'] if e['status'] == 'moved']
        self.assertEqual(len(moved), 2)
        self.assertEqual(checks.check_folder(self.root)['failed_checks'], 0)

    def test_neither_archive_nor_step_is_refused(self):
        with self.assertRaises(ValueError):
            Exporter(self.root, self.adapter).run(fake.data(self.files), archive=False, step=False)

    def test_the_launcher_says_the_same_note(self):
        import importlib.util
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('launcher_for_step_only', root / 'scripts' / 'autorun.py')
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        self.assertEqual(launcher.ARCHIVE_OFF_NOTE, core.ARCHIVE_OFF_NOTE)

    def test_autorun_settings(self):
        settings = autorun.check_settings(dict(archive=False))
        self.assertIs(settings[7], False)  # archive, after step
        self.assertIs(autorun.check_settings({})[7], True)
        for bad in (dict(archive='no'), dict(archive=False, step=False), dict(archive=False, f3z_bundles=True)):
            with self.assertRaises(ValueError, msg=bad):
                autorun.check_settings(bad)


if __name__ == '__main__':
    unittest.main()
