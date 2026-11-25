"""Which way is up: each exported design's manifest entry records up_axis ("y", "z", or null with
up_axis_error), read only from the design's own Top and Front named views. Entries written before
the field existed read as unknown, and nothing else changes."""
import csv
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest

from tests import fake_adsk as fake

fake.install()
from fusion_bulk_export import report, upaxis  # noqa: E402
from fusion_bulk_export.core import Exporter  # noqa: E402
from fusion_bulk_export.fusion import FusionAdapter  # noqa: E402


def design(mode):
    """A design with the named views of fake.File.up_axis mode (none for 'missing')."""
    result = NS()
    views = fake.named_views(mode)
    if views is not None:
        result.namedViews = views
    return result


def views(top, front):
    """A design whose Top camera is (eye, target) and Front up vector is front."""
    return NS(namedViews=fake.NamedViews(fake.NamedView(fake.Camera(top[0], top[1], (0, 1, 0))),
                                         fake.NamedView(fake.Camera((0, -10, 0), (0, 0, 0), front))))


class UpAxisTests(unittest.TestCase):
    def setUp(self):
        del fake.CALLS[:]

    def test_z_up_and_y_up(self):
        self.assertEqual(upaxis.up_axis(design('z')), ('z', None))
        self.assertEqual(upaxis.up_axis(design('y')), ('y', None))

    def test_a_tolerance_not_exact_vectors(self):
        # Slightly off the axis still counts; units and distance do not matter.
        self.assertEqual(upaxis.up_axis(views(((0.2, 0.1, 55), (0, 0, 3)), (0.1, -0.2, 3))), ('z', None))
        self.assertEqual(upaxis.up_axis(views(((0, 4, 0.3), (0, -1, 0)), (0.3, 1, 0))), ('y', None))

    def test_views_that_disagree_or_say_neither_are_unknown(self):
        self.assertEqual(upaxis.up_axis(design('disagree')), (None, 'the Top view says Z up, the Front view Y up'))
        self.assertEqual(upaxis.up_axis(design('tilted')), (None, 'the Top view does not look down Y or Z'))
        # Looking up from below, along X, or with a zero direction: not a Top view this reads.
        self.assertEqual(upaxis.up_axis(views(((0, 0, -10), (0, 0, 0)), (0, 0, 1)))[0], None)
        self.assertEqual(upaxis.up_axis(views(((10, 0, 0), (0, 0, 0)), (0, 0, 1)))[0], None)
        self.assertEqual(upaxis.up_axis(views(((1, 2, 3), (1, 2, 3)), (0, 0, 1)))[0], None)
        self.assertEqual(upaxis.up_axis(views(((0, 0, 10), (0, 0, 0)), (1, 0, 0))),
                         (None, "the Front view's up is neither +Y nor +Z"))
        self.assertEqual(upaxis.up_axis(views(((0, 0, 10), (0, 0, 0)), (0, 0, -1)))[0], None)

    def test_missing_views_or_a_raising_api_are_unknown_with_a_reason(self):
        self.assertEqual(upaxis.up_axis(design('missing')), (None, 'the design has no named views'))
        self.assertEqual(upaxis.up_axis(design('no_front')), (None, 'the design has no Front view'))
        self.assertEqual(upaxis.up_axis(design('raise')),
                         (None, 'named views unreadable: 2 : InternalValidationError : namedViews'))
        no_camera = NS(namedViews=fake.NamedViews(NS(camera=None), NS(camera=None)))
        self.assertEqual(upaxis.up_axis(no_camera), (None, 'the Top view has no camera'))
        broken = NS(namedViews=fake.NamedViews(NS(camera=NS(eye=None, target=None)), None))
        self.assertEqual(upaxis.up_axis(broken)[0], None)

    def test_only_reads_the_views(self):
        for mode in ('z', 'y', 'disagree'):
            upaxis.up_axis(design(mode))
        self.assertEqual(fake.CALLS, [])
        with self.assertRaises(fake.AccountWrite):  # the fake does catch a change
            fake.named_views('z').topNamedView.camera = fake.Camera((0, 0, 1), (0, 0, 0), (0, 1, 0))


class UpAxisExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.adapter = FusionAdapter(NS(documents=fake.Documents()))
        del fake.CALLS[:]

    def files(self):
        modes = ('z', 'y', 'disagree', 'raise')
        result = []
        for mode in modes:
            file = fake.File('Part ' + mode, 'part-' + mode)
            file.up_axis = mode
            result.append(file)
        return result

    def run_export(self, files, **kwargs):
        exporter = Exporter(self.root, self.adapter)
        self.counts = exporter.run(fake.data(files), **kwargs)
        self.run_info = exporter.run_info
        self.manifest = json.loads((self.root / 'manifest.json').read_bytes().decode('utf-8'))
        return {entry['id']: entry for entry in self.manifest['entries']}

    def check_entries(self, entries):
        self.assertEqual((entries['part-z']['up_axis'], entries['part-y']['up_axis']), ('z', 'y'))
        self.assertNotIn('up_axis_error', entries['part-z'])
        self.assertNotIn('up_axis_error', entries['part-y'])
        for key, reason in (('part-disagree', 'the Top view says Z up, the Front view Y up'),
                            ('part-raise', 'named views unreadable: 2 : InternalValidationError : namedViews')):
            self.assertEqual(entries[key]['status'], 'done')  # an unknown axis never stops an export
            self.assertIsNone(entries[key]['up_axis'])
            self.assertEqual(entries[key]['up_axis_error'], reason)
        self.assertEqual(fake.CALLS, [])

    def test_every_exported_design_records_its_up_axis(self):
        self.check_entries(self.run_export(self.files()))

    def test_step_only_exports_record_it_too(self):
        entries = self.run_export(self.files(), archive=False)
        self.assertEqual(entries['part-z']['archive'], 'off')
        self.check_entries(entries)

    def test_a_design_exported_again_drops_the_old_reason(self):
        # Exported without STEP, then again with it (the same version, the same entry): the
        # earlier run's up_axis_error must not stay beside the new answer.
        file = fake.File('Part', 'part')
        file.up_axis = 'raise'
        self.assertIn('up_axis_error', self.run_export([file], step=False)['part'])
        file.up_axis = 'y'
        entries = self.run_export([file])
        self.assertEqual(self.counts['exported'], 1)
        self.assertEqual(entries['part']['up_axis'], 'y')
        self.assertNotIn('up_axis_error', entries['part'])

    def test_an_up_to_date_design_keeps_its_entry(self):
        file = fake.File('Part', 'part')
        file.up_axis = 'y'
        self.run_export([file])
        file.up_axis = 'z'  # not opened again: its entry stays as it was
        entries = self.run_export([file])
        self.assertEqual(self.counts['up_to_date'], 1)
        self.assertEqual(entries['part']['up_axis'], 'y')

    def test_an_old_manifest_without_the_field_reads_as_unknown(self):
        file = fake.File('Part', 'part')
        file.up_axis = 'y'
        self.run_export([file])
        old = json.loads((self.root / 'manifest.json').read_bytes())
        for entry in old['entries']:
            entry.pop('up_axis', None)
            entry.pop('up_axis_error', None)
        (self.root / 'manifest.json').write_bytes(json.dumps(old).encode('utf-8'))
        entries = self.run_export([file])
        self.assertEqual(self.counts['up_to_date'], 1)
        self.assertNotIn('up_axis', entries['part'])  # not opened again: stays unknown
        rows = list(csv.reader(io.StringIO(report.render_csv(self.manifest, self.run_info))))
        self.assertEqual(rows[0][-1], 'up_axis')
        self.assertEqual(rows[1][-1], '')
        self.assertNotIn('Y up', report.render_html(self.manifest, self.run_info, self.counts))


class UpAxisReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_csv_column_and_quiet_html(self):
        files = []
        for mode in ('z', 'y', 'raise'):
            file = fake.File('Part ' + mode, 'part-' + mode)
            file.up_axis = mode
            files.append(file)
        exporter = Exporter(self.root, FusionAdapter(NS(documents=fake.Documents())))
        counts = exporter.run(fake.data(files))
        manifest = json.loads((self.root / 'manifest.json').read_bytes().decode('utf-8'))
        rows = list(csv.DictReader(io.StringIO(report.render_csv(manifest, exporter.run_info))))
        self.assertEqual(sorted((row['design'], row['up_axis']) for row in rows),
                         [('Part raise', ''), ('Part y', 'y'), ('Part z', 'z')])
        header = report.render_csv(manifest, exporter.run_info).splitlines()[0].split(',')
        self.assertEqual(header[-1], 'up_axis')
        page = report.render_html(manifest, exporter.run_info, counts)
        self.assertEqual(page.count('Y up'), 1)
        self.assertNotIn('Z up', page)
        self.assertIn('Part y v3 <span class="muted">Y up</span>', page)
        # index.csv and index.html as written into the folder say the same.
        report.write(exporter, counts)
        written = (self.root / 'index.csv').read_text(encoding='utf-8-sig')
        self.assertEqual(written.splitlines()[0].split(',')[-1], 'up_axis')
        self.assertEqual((self.root / 'index.html').read_text(encoding='utf-8').count('Y up'), 1)


if __name__ == '__main__':
    unittest.main()
