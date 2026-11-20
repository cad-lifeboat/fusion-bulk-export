"""index.html / index.csv, the end-of-run summary text and the opener."""
import csv
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest

from tests import fake_adsk as fake

fake.install()
from fusion_bulk_export import report


def entry(name, status, error=None, files=(), path=()):
    return dict(hub='Hub', project='Proj', path=list(path), name=name, version=2, status=status, error=error,
                files=[dict(path=p, sha256='x', bytes=1) for p in files])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = dict(entries=[
            entry('Good <b>', 'done', files=['Hub/Proj/Größen/Good _b_ v2.f3d', 'Hub/Proj/Größen/Good _b_ v2.step'],
                  path=['Größen']),
            entry('Drawing', 'skipped', 'Not a Fusion design: f2d'),
            entry('Bad', 'failed', 'Synthetic export failure'),
            entry('Open one', 'failed', 'This design is open in Fusion with unsaved changes; save or close it'),
        ])
        self.run_info = dict(finished='2026-01-01T00:00:00+00:00', cancelled=False, stopped_reason=None,
                             discovery_errors=[dict(path=['Hub', 'Proj', 'Locked'], error='No access')],
                             counts=dict(exported=1, up_to_date=0, skipped=1, failed=2))

    def write(self):
        exporter = NS(root=self.root, manifest=self.manifest, run_info=self.run_info)
        return report.write(exporter)

    def test_html_lists_failures_first_with_next_step_and_relative_links(self):
        page, _ = self.write()
        text = page.read_text(encoding='utf-8')
        self.assertLess(text.index('Synthetic export failure'), text.index('Not a Fusion design'))
        self.assertLess(text.index('No access'), text.index('Not a Fusion design'))
        self.assertIn('Save or close this design in Fusion', text)
        self.assertIn('href="Hub/Proj/Gr%C3%B6%C3%9Fen/Good%20_b_%20v2.f3d"', text)
        self.assertIn('Good &lt;b&gt; v2', text)
        self.assertNotIn('<script', text)
        self.assertNotIn('http', text)
        self.assertIn('prefers-color-scheme: dark', text)
        self.assertIn('Failed: <b>2</b>', text)

    def test_csv_has_one_row_per_item(self):
        _, table = self.write()
        rows = list(csv.DictReader(io.StringIO(table.read_text(encoding='utf-8-sig'))))
        self.assertEqual([r['status'] for r in rows], ['failed', 'failed', 'unreadable', 'skipped', 'done'])
        self.assertEqual(rows[-1]['step'], 'Hub/Proj/Größen/Good _b_ v2.step')
        self.assertEqual(rows[-1]['folder'], 'Größen')

    def test_summary_text(self):
        text = report.summary_text(self.run_info['counts'], self.run_info, self.manifest, '/out')
        self.assertTrue(text.startswith('Export finished.'))
        self.assertIn('1 exported now, 0 already up to date, 1 skipped (not a design), 2 failed.', text)
        self.assertEqual(text.count('\n- '), 3)
        self.assertIn('Run again to retry the 2 failed.', text)
        self.assertIn('1 folder(s) could not be read', text)
        info = dict(self.run_info, stopped_reason='The disk is full.', stopped_code='disk_full',
                    low_space=True, free_bytes=50 << 20)
        text = report.summary_text(dict(exported=0, up_to_date=0, skipped=0, failed=0), info, dict(entries=[]), '/o')
        self.assertTrue(text.startswith('The export stopped: The disk is full.'))
        self.assertIn('Free some disk space', text)
        self.assertIn('Low disk space: 50 MB free.', text)
        self.assertNotIn('Run again to retry', text)

    def test_warnings_in_report_csv_and_summary(self):
        warned = entry('Hollow', 'done', files=['Hub/Proj/Hollow v2.f3d', 'Hub/Proj/Hollow v2.step'])
        warned['warnings'] = ['STEP has no bodies (Fusion showed 1)']
        self.manifest['entries'].append(warned)
        page, table = self.write()
        text = page.read_text(encoding='utf-8')
        self.assertIn('Warning: STEP has no bodies (Fusion showed 1)', text)
        self.assertIn('Exported, with a warning', text)
        self.assertLess(text.index('Hollow'), text.index('Good &lt;b&gt;'))
        rows = list(csv.DictReader(io.StringIO(table.read_text(encoding='utf-8-sig'))))
        self.assertEqual([r['warnings'] for r in rows if r['design'] == 'Hollow'],
                         ['STEP has no bodies (Fusion showed 1)'])
        summary = report.summary_text(self.run_info['counts'], self.run_info, self.manifest, '/out')
        self.assertIn('1 exported with a warning', summary)
        self.assertIn('- Hollow: STEP has no bodies (Fusion showed 1)', summary)

    def test_empty_folder_report(self):
        self.manifest = dict(entries=[])
        self.run_info['discovery_errors'] = []
        page, _ = self.write()
        self.assertIn('Nothing exported yet', page.read_text())

    def test_open_path_per_platform(self):
        calls = []
        report.open_path('/x', platform='darwin', run=calls.append)
        report.open_path('C:/x', platform='win32', startfile=lambda p: calls.append(('start', p)))
        report.open_path('/y', platform='linux', run=calls.append)
        self.assertEqual(calls, [['open', '/x'], ('start', 'C:/x'), ['xdg-open', '/y']])


if __name__ == '__main__':
    unittest.main()
