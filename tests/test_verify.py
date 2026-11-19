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

    def build(self, step=STEP, solids=2, archive='.f3z'):
        with zipfile.ZipFile(self.root / ('a' + archive), 'w') as out:
            out.writestr('Manifest.json', '{}')
            out.writestr('part.f3d', 'x')
            out.writestr('Manifest.dat', 'x')
            out.writestr('FusionAssetName[Active]/x', 'x')
        (self.root / 'a.step').write_text(step)
        entry = dict(id='f1', name='Secret Name', status='done',
                     files=[dict(path='a' + archive), dict(path='a.step')])
        (self.root / 'manifest.json').write_text(json.dumps(dict(entries=[entry])))
        (self.root / 'selftest-result.json').write_text(json.dumps(dict(designs=[dict(id='f1', solid_bodies=solids)])))

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
        manifest = json.loads((self.root / 'manifest.json').read_text())
        manifest['entries'][0]['solid_bodies'] = 2
        (self.root / 'selftest-result.json').unlink()
        manifest['entries'].append(dict(id='d', name='Drawing', status='skipped',
                                        error='Not a Fusion design: f2d', files=[]))
        (self.root / 'manifest.json').write_text(json.dumps(manifest))
        status, text = self.run_check()
        self.assertEqual(status, 0, text)
        self.assertIn('"not_designs": 1', text)
        self.assertIn('"solids_match": 1', text)

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
        self.build(step=STEP.replace('END-ISO-10303-21;', ''), solids=3)
        status, text = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn('truncated', text)
        self.assertIn('NOTE STEP has 2 solids, Fusion reported 3', text)

    def test_solid_mismatch_is_a_note_unless_strict(self):
        self.build(solids=3)
        status, text = self.run_check()
        self.assertEqual(status, 0, text)
        self.assertIn('"solids_differ": 1', text)
        status, text = self.run_check('--strict')
        self.assertEqual(status, 1)
        self.assertIn('PROBLEM STEP has 2 solids', text)


if __name__ == '__main__':
    unittest.main()
