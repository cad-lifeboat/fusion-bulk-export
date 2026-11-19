"""Release zip: one add-in folder, no run-time files or caches."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('package', Path(__file__).parent.parent / 'scripts' / 'package.py')
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class PackageTests(unittest.TestCase):
    def test_zip_holds_the_add_in_only(self):
        with tempfile.TemporaryDirectory() as temp:
            target = package.build(Path(temp))
            names = zipfile.ZipFile(target).namelist()
        self.assertTrue(all(name.startswith('FusionBulkExport/') for name in names))
        for required in ('FusionBulkExport.py', 'FusionBulkExport.manifest', 'LICENSE', 'resources/32x32.png',
                         'fusion_bulk_export/core.py', 'fusion_bulk_export/autorun.py'):
            self.assertIn('FusionBulkExport/' + required, names)
        self.assertFalse(any('__pycache__' in name or name.endswith('.pyc') or 'autorun.json' in name
                             for name in names))


if __name__ == '__main__':
    unittest.main()
