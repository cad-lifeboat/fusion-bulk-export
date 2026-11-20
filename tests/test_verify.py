"""Output checker on synthetic export folders."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
import zipfile

spec = importlib.util.spec_from_file_location(
    'verify_export', Path(__file__).parent.parent / 'scripts' / 'verify-export.py')
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)

STEP = ('ISO-10303-21;\nHEADER;\nFILE_SCHEMA((\'AUTOMOTIVE_DESIGN\'));\nENDSEC;\nDATA;\n'
        + '#1=MANIFOLD_SOLID_BREP(\'\',#2);\n#3=MANIFOLD_SOLID_BREP(\'\',#4);\n'
        + '/* padding */\n' * 200 + 'ENDSEC;\nEND-ISO-10303-21;\n')


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def build(self, step=STEP, solids=2, archive='.f3z', distinct=None):
        with zipfile.ZipFile(self.root / ('a' + archive), 'w') as out:
            out.writestr('Manifest.json', '{}')
            out.writestr('part.f3d', 'x')
            out.writestr('Manifest.dat', 'x')
            out.writestr('FusionAssetName[Active]/x', 'x')
        (self.root / 'a.step').write_text(step)
        entry = dict(id='f1', name='Secret Name', status='done', visible_solid_bodies=solids,
                     files=[dict(path='a' + archive), dict(path='a.step')])
        if distinct is not None:
            entry['visible_solid_bodies_distinct'] = distinct
        (self.root / 'manifest.json').write_text(json.dumps(dict(entries=[entry])))

    def run_check(self, *flags):
        stream = io.StringIO()
        with redirect_stdout(stream):
            status = verify.main([str(self.root), *flags])
        return status, stream.getvalue()

    def test_valid_export_passes_without_printing_names(self):
        self.build()
        status, text = self.run_check()
        self.assertEqual(status, 0, text)
        self.assertNotIn('Secret Name', text)
        self.assertIn('"solids_match": 1', text)

    def test_names_flag_shows_names(self):
        self.build()
        self.assertIn('Secret Name', self.run_check('--names')[1])

    def test_manifest_solid_count_and_skipped_drawings(self):
        self.build(solids=2)
        manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        del manifest['entries'][0]['visible_solid_bodies']
        manifest['entries'][0]['solid_bodies'] = 2
        manifest['entries'].append(dict(id='d', name='Drawing', status='skipped',
                                        error='Not a Fusion design: f2d', files=[]))
        (self.root / 'manifest.json').write_text(json.dumps(manifest))
        status, text = self.run_check()
        self.assertEqual(status, 0, text)
        self.assertIn('"not_designs": 1', text)
        self.assertIn('"solids_match": 1', text)

    def test_unsupported_files_are_not_failures(self):
        self.build()
        manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        manifest['entries'].append(dict(id='b', name='Board', kind='file', status='unsupported', files=[],
                                        error='Fusion cannot export this file type through its API (.fbrd)'))
        (self.root / 'manifest.json').write_text(json.dumps(manifest))
        status, text = self.run_check()
        self.assertEqual(status, 0, text)
        self.assertIn('"unsupported": 1', text)
        self.assertIn('"failed_checks": 0', text)

    def test_plain_archive_passes(self):
        self.build(archive='.f3d')
        self.assertEqual(self.run_check()[0], 0)

    def test_unsupported_member_compression_is_counted_not_fatal(self):
        self.build(archive='.f3d')
        with zipfile.ZipFile(self.root / 'a.f3d') as archive:
            first = archive.infolist()[0].filename
        real_open = zipfile.ZipFile.open

        def fussy(archive, member, *args, **kwargs):
            name = member.filename if isinstance(member, zipfile.ZipInfo) else member
            if name == first:
                raise NotImplementedError('That compression method is not supported')
            return real_open(archive, member, *args, **kwargs)
        with unittest.mock.patch.object(zipfile.ZipFile, 'open', fussy):
            status, text = self.run_check()
        self.assertEqual(status, 0, text)
        self.assertIn('"members_not_crc_checked": 1', text)

    def test_truncated_step_and_solid_mismatch_fail(self):
        self.build(step=STEP.replace('END-ISO-10303-21;', ''), solids=4, distinct=3)
        status, text = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn('truncated', text)
        self.assertIn('NOTE STEP has 2 solids, fewer than the 3 distinct ones Fusion showed', text)

    def test_solid_mismatch_is_a_note_unless_strict(self):
        self.build(solids=3, distinct=3)
        status, text = self.run_check()
        self.assertEqual(status, 0, text)
        self.assertIn('"solids_differ": 1', text)
        status, text = self.run_check('--strict')
        self.assertEqual(status, 1)
        self.assertIn('PROBLEM STEP has 2 solids', text)

    def test_a_count_between_fusions_counts_is_only_noted(self):
        # Seen on real Fusion: STEP files with about twice Fusion's count, or between its counts.
        for solids, distinct in ((1, None), (1, 1), (4, 1)):  # the STEP holds 2
            self.build(solids=solids, distinct=distinct)
            status, text = self.run_check('--strict')
            self.assertEqual(status, 0, text)
            self.assertIn('"solids_differ": 0', text)
            self.assertIn('"solids_noted": 1', text)
            self.assertNotIn('NOTE', text)


class CheckFolderTests(unittest.TestCase):
    """Check this folder on a real (synthetic) export: hashes, journal, no Fusion needed."""
    def setUp(self):
        import sys
        from tests import fake_adsk as fake
        fake.install()
        from types import SimpleNamespace as NS
        from fusion_bulk_export.core import Exporter
        from fusion_bulk_export.fusion import FusionAdapter
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake = fake
        self.exporter = Exporter(self.root, FusionAdapter(NS(documents=fake.Documents())))
        empty = fake.File('Empty', 'empty')
        empty.step_solids = empty.step_hidden_solids = 0  # nothing at all, hidden bodies included
        self.exporter.run(fake.data([fake.File('A', 'a'), empty]))
        self.manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))

    def test_clean_export_checks_out_and_reports_the_empty_step(self):
        from fusion_bulk_export import checks
        shown = []
        totals = checks.check_folder(self.root, show=lambda *args: shown.append(args))
        self.assertEqual(totals['failed_checks'], 0)
        self.assertEqual((totals['done'], totals['designs'], totals['warnings']), (2, 2, 1))
        self.assertEqual(totals['solids_differ'], 1)
        notes = [n for args in shown for n in args[4]]
        self.assertIn('STEP has no bodies (2 solid bodies in the design)', notes)
        self.assertEqual(checks.check_folder(self.root, strict=True)['failed_checks'], 1)

    def test_changed_and_missing_files_are_problems(self):
        from fusion_bulk_export import checks
        first, second = self.manifest['entries']
        path = self.root / first['files'][1]['path']
        data = path.read_bytes()
        path.write_bytes(data.replace(b'synthetic padding', b'synthetic paddinG'))  # same size, other bytes
        (self.root / second['files'][0]['path']).unlink()
        totals = checks.check_folder(self.root)
        self.assertEqual((totals['changed'], totals['missing'], totals['failed_checks']), (1, 1, 2))

    def test_items_still_in_the_journal_are_checked(self):
        from fusion_bulk_export import checks
        entry = dict(self.manifest['entries'][0], key='["x"]', files=[dict(path='gone.step', bytes=1, sha256='0')])
        (self.root / 'manifest.journal').write_text(json.dumps(dict(entry=entry)) + '\n')
        self.assertEqual(checks.check_folder(self.root)['missing'], 1)

    def test_verify_script_wraps_the_check(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            status = verify.main([str(self.root)])
        self.assertEqual(status, 0, stream.getvalue())
        self.assertIn('NOTE STEP has no bodies', stream.getvalue())
        self.assertNotIn('Empty', stream.getvalue())


if __name__ == '__main__':
    unittest.main()
