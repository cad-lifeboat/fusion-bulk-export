"""manifest.json names its documented schema (schema_id) and who wrote it (exporter) on every
write; a folder without them (older versions) reads as cad-export-manifest/1, and one naming a
schema this version does not know is refused, never guessed at."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest

from tests import fake_adsk as fake
from fusion_bulk_export import checks
from fusion_bulk_export.core import Exporter

fake.install()
from fusion_bulk_export.fusion import FusionAdapter  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class ManifestSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.adapter = FusionAdapter(NS(documents=fake.Documents()))

    def manifest(self):
        return json.loads((self.root / 'manifest.json').read_bytes().decode('utf-8'))

    def test_every_write_names_schema_and_exporter_first(self):
        Exporter(self.root, self.adapter).run(fake.data([fake.File('Part', 'part')]))
        manifest = self.manifest()
        version = json.loads((ROOT / 'FusionBulkExport' / 'FusionBulkExport.manifest').read_bytes())['version']
        self.assertEqual(list(manifest)[:3], ['schema', 'schema_id', 'exporter'])
        self.assertEqual(manifest['schema_id'], 'cad-export-manifest/1')
        self.assertEqual(manifest['exporter'], dict(name='fusion-bulk-export', version=version))

    def test_a_folder_without_schema_id_is_read_and_stamped(self):
        Exporter(self.root, self.adapter).run(fake.data([fake.File('Part', 'part')]))
        old = self.manifest()
        del old['schema_id'], old['exporter']
        (self.root / 'manifest.json').write_bytes(json.dumps(old).encode('utf-8'))
        self.assertEqual(checks.check_folder(self.root)['failed_checks'], 0)
        counts = Exporter(self.root, self.adapter).run(fake.data([fake.File('Part', 'part')]))
        self.assertEqual(counts['up_to_date'], 1)
        self.assertEqual(self.manifest()['schema_id'], 'cad-export-manifest/1')

    def test_an_unknown_schema_id_is_refused_by_every_reader(self):
        (self.root / 'manifest.json').write_bytes(json.dumps(
            dict(schema=2, schema_id='cad-export-manifest/2', runs=[], entries=[])).encode('utf-8'))
        with self.assertRaises(checks.UnknownSchema):
            Exporter(self.root, self.adapter)
        with self.assertRaises(checks.UnknownSchema):
            checks.check_folder(self.root)
        from fusion_bulk_export import f3z
        with self.assertRaises(ValueError):
            f3z.bundle_folder(self.root)
        self.assertFalse((self.root / 'export.lock').exists())

    def test_the_bundle_command_stamps_too(self):
        Exporter(self.root, self.adapter).run(fake.data([fake.File('Part', 'part')]))
        old = self.manifest()
        del old['schema_id'], old['exporter']
        (self.root / 'manifest.json').write_bytes(json.dumps(old).encode('utf-8'))
        from fusion_bulk_export import f3z
        f3z.bundle_folder(self.root)
        self.assertEqual(self.manifest()['exporter']['name'], 'fusion-bulk-export')

    def test_check_command_reports_an_unknown_schema_in_one_line(self):
        import contextlib
        import importlib.util
        import io
        spec = importlib.util.spec_from_file_location('launcher_for_schema', ROOT / 'scripts' / 'autorun.py')
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        (self.root / 'manifest.json').write_bytes(json.dumps(
            dict(schema=2, schema_id='cad-export-manifest/9', runs=[], entries=[])).encode('utf-8'))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(launcher.check(self.root), 1)
        self.assertIn('format this version does not know', out.getvalue())


if __name__ == '__main__':
    unittest.main()
