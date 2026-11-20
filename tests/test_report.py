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
                             counts=dict(exported=1, up_to_date=0, files=0, skipped=1, failed=2))

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
        text = report.summary_text(self.run_info['counts'], self.run_info, self.manifest, '/out', '/out/index.html')
        self.assertTrue(text.startswith('Finished\n'))
        self.assertIn('Exported now  1\nSkipped       1\nFailed        2\n', text)
        self.assertEqual(text.count('\n- '), 3)
        self.assertIn('Run again to retry the 2 failed.', text)
        self.assertIn('1 folder(s) could not be read', text)
        self.assertTrue(text.endswith('Folder: /out\nReport: /out/index.html'))
        info = dict(self.run_info, stopped_reason='The disk is full.', stopped_code='disk_full',
                    low_space=True, free_bytes=50 << 20, discovery_errors=[])
        text = report.summary_text(dict(exported=0, up_to_date=0, files=0, skipped=0, failed=0), info, dict(entries=[]), '/o')
        self.assertTrue(text.startswith('Stopped: The disk is full.\n'))
        self.assertIn('Free some disk space', text)
        self.assertIn('Low disk space: 50 MB free.', text)
        self.assertNotIn('Run again to retry', text)
        self.assertNotIn('Report:', text)  # not written

    def test_summary_rows_leave_out_zeros_except_exported_now(self):
        zero = dict(exported=0, up_to_date=0, files=0, files_up_to_date=0, skipped=0, failed=0)
        self.assertEqual(report.summary(zero, {}, dict(entries=[]), '/o')['rows'], [['Exported now', 0]])
        every = dict(exported=1, up_to_date=2, files=3, files_up_to_date=4, skipped=5, failed=6)
        warned = entry('Thin', 'done')
        warned['warnings'] = ['STEP has no bodies']
        rows = report.summary(every, {}, dict(entries=[warned]), '/o')['rows']
        self.assertEqual(rows, [['Exported now', 1], ['Already up to date', 2], ['Other files saved', 3],
                                ['Other files up to date', 4], ['Skipped', 5], ['Failed', 6], ['Warnings', 1]])

    def test_summary_names_three_failures_then_counts_the_rest(self):
        manifest = dict(entries=[entry('D%d' % n, 'failed', 'reason %d' % n) for n in range(7)])
        counts = dict(exported=0, failed=7)
        data = report.summary(counts, {}, manifest, '/o')
        self.assertEqual([f['name'] for f in data['failures']], ['D0', 'D1', 'D2'])
        self.assertEqual(data['more'], 4)
        self.assertEqual(data['retry'], 'Run again to retry the 7 failed.')
        self.assertIn('\nand 4 more in the report.\n', report.text_of(data))
        exactly = report.summary(dict(exported=0, failed=3), {}, dict(entries=manifest['entries'][:3]), '/o')
        self.assertEqual((len(exactly['failures']), exactly['more']), (3, 0))
        self.assertNotIn('more in the report', report.text_of(exactly))

    def test_retry_hint_only_with_failures(self):
        none = report.summary(dict(exported=3, failed=0), {}, dict(entries=[]), '/o')
        self.assertEqual(none['retry'], '')
        self.assertNotIn('Run again to retry', report.text_of(none))
        self.assertEqual(report.summary(dict(exported=3, failed=1), {}, dict(entries=[]), '/o')['retry'],
                         'Run again to retry the 1 failed.')

    def test_summary_headlines(self):
        counts = dict(exported=0)
        self.assertEqual(report.summary(counts, {}, dict(entries=[]), '/o')['headline'], 'Finished')
        cancelled = report.summary(counts, dict(cancelled=True), dict(entries=[]), '/o')
        self.assertEqual(cancelled['headline'], 'Cancelled')
        self.assertIn('Run again to carry on where it stopped.', cancelled['notes'])
        stopped = report.summary(counts, dict(stopped_reason='The output folder is gone.'), dict(entries=[]), '/o')
        self.assertEqual(stopped['headline'], 'Stopped: The output folder is gone.')

    def test_summary_notes_long_paths_and_low_disk(self):
        long = entry('Deep', 'done')
        long['long_path'] = True
        data = report.summary(dict(exported=1), dict(low_space=True, free_bytes=300 << 20), dict(entries=[long]), '/o')
        self.assertIn('Low disk space: 300 MB free.', data['notes'])
        self.assertIn(report.LONG_PATHS % 1, data['notes'])
        plain = report.summary(dict(exported=1), {}, dict(entries=[entry('Short', 'done')]), '/o')
        self.assertEqual(plain['notes'], [])

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
        rows = report.summary(self.run_info['counts'], self.run_info, self.manifest, '/out')['rows']
        self.assertIn(['Warnings', 1], rows)

    def test_thumbnail_shown_and_listed(self):
        self.manifest['entries'][0]['thumbnail'] = dict(path='Hub/Proj/Größen/Good _b_ v2.png')
        page, table = self.write()
        self.assertIn('<img class="thumb" src="Hub/Proj/Gr%C3%B6%C3%9Fen/Good%20_b_%20v2.png"',
                      page.read_text(encoding='utf-8'))
        rows = list(csv.DictReader(io.StringIO(table.read_text(encoding='utf-8-sig'))))
        self.assertEqual(rows[-1]['thumbnail'], 'Hub/Proj/Größen/Good _b_ v2.png')

    def test_other_files_have_their_own_table_and_count(self):
        pdf = entry('Spec', 'done', files=['Hub/Proj/Spec.pdf'])
        pdf['kind'] = 'file'
        drawing = entry('Drawing', 'skipped', 'Fusion cannot export this file type through its API (.f2d; '
                                              'fusion did not download it)')
        drawing['kind'] = 'file'
        self.manifest['entries'] += [pdf, drawing]
        self.run_info['counts'] = dict(exported=1, up_to_date=0, files=1, skipped=2, failed=2)
        page, table = self.write()
        text = page.read_text(encoding='utf-8')
        self.assertIn('<h2>Other files</h2>', text)
        self.assertLess(text.index('<h2>Designs</h2>'), text.index('<h2>Other files</h2>'))
        self.assertLess(text.index('<h2>Other files</h2>'), text.index('Spec'))
        self.assertIn('Other files saved: <b>1</b>', text)
        self.assertIn('href="Hub/Proj/Spec.pdf">PDF</a>', text)
        self.assertIn('open it in Fusion and export or download it by hand', text)
        self.assertIn('1 other files saved, 1 not saved', text)
        rows = list(csv.DictReader(io.StringIO(table.read_text(encoding='utf-8-sig'))))
        spec = next(r for r in rows if r['design'] == 'Spec')
        self.assertEqual((spec['kind'], spec['file'], spec['archive']), ('file', 'Hub/Proj/Spec.pdf', ''))
        self.assertEqual(next(r for r in rows if r['design'] == 'Bad')['kind'], 'design')
        rows = report.summary(self.run_info['counts'], self.run_info, self.manifest, '/out')['rows']
        self.assertEqual(rows, [['Exported now', 1], ['Other files saved', 1], ['Skipped', 2], ['Failed', 2]])

    def test_designs_only_skip_says_how_to_save_it(self):
        self.assertIn('Also save other files', report.next_step('skipped', 'Not a Fusion design: pdf'))
        self.assertIn('not a 3D design', report.next_step('skipped', 'No Fusion Design product (drawing)'))
        self.assertIn('export its PDF by hand', report.next_step('skipped', 'Fusion did not write the drawing PDF'))
        self.assertEqual(report.next_step('skipped', 'This drawing is open in Fusion with unsaved changes; save or '
                                                     'close it, then run again.'),
                         'Save or close it in Fusion, then export again.')

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


class LinkReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        broken = entry('Assembly', 'failed', 'A linked design is missing or cannot be opened (2 links).')
        broken.update(has_references=True, references=[dict(id='a'), dict(id='b')], fusion_error='5 : not resolved')
        stale = entry('Older', 'done', files=['Hub/Proj/Older v2.f3d', 'Hub/Proj/Older v2.step'])
        stale.update(has_references=True, references_out_of_date=True, references=[dict(id='c')])
        self.manifest = dict(entries=[broken, stale, entry('Plain', 'done', files=['Hub/Proj/Plain v2.f3d'])])
        self.run_info = dict(finished='t', cancelled=False, stopped_reason=None, discovery_errors=[])

    def test_summary_and_report_name_the_link_problem(self):
        counts = dict(exported=2, up_to_date=0, files=0, skipped=0, failed=1)
        text = report.summary_text(counts, self.run_info, self.manifest, '/out')
        self.assertIn('- Assembly: A linked design is missing or cannot be opened (2 links).', text)
        self.assertNotIn('not resolved', text)  # Fusion's own text is in the report, not the summary
        self.assertIn('1 design(s) use older versions of linked designs (a note, not a failure).', text)
        page = report.render_html(self.manifest, self.run_info, counts)
        self.assertIn('Fusion said: 5 : not resolved', page)
        self.assertIn('fix its links', page)
        self.assertIn('1 with out-of-date links (a note, not a failure)', page)
        self.assertIn('Note: Some of its links are out of date', page)
        rows = list(csv.DictReader(io.StringIO(report.render_csv(self.manifest, self.run_info))))
        by_name = {row['design']: row for row in rows}
        self.assertEqual((by_name['Assembly']['has_links'], by_name['Assembly']['links'],
                          by_name['Assembly']['fusion_error']), ('yes', '2', '5 : not resolved'))
        self.assertEqual(by_name['Older']['links_out_of_date'], 'yes')
        self.assertEqual((by_name['Plain']['has_links'], by_name['Plain']['links']), ('', ''))
