"""Output checker on synthetic export folders."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
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

    def test_plain_archive_passes(self):
        self.build(archive='.f3d')
        self.assertEqual(self.run_check()[0], 0)

    def test_truncated_step_and_solid_mismatch_fail(self):
        self.build(step=STEP.replace('END-ISO-10303-21;', ''), solids=3)
        status, text = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn('truncated', text)
        self.assertIn('Fusion reported 3', text)


if __name__ == '__main__':
    unittest.main()
