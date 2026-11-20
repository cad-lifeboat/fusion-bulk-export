"""The entry point must import with only its own file location to go on, as inside Fusion."""
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class EntryImportTests(unittest.TestCase):
    def test_imports_from_any_working_directory_without_path_setup(self):
        script = '''
import importlib.util, sys
sys.path.insert(0, %r)
from tests import fake_adsk
fake_adsk.install()
import adsk.core
adsk.core.CommandEventHandler = adsk.core.CommandCreatedEventHandler = object
# Undo the test package's own path setup: Fusion provides nothing but the file.
sys.path[:] = [p for p in sys.path if not p.endswith('FusionBulkExport')]
spec = importlib.util.spec_from_file_location('FusionBulkExport', %r)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert hasattr(module, 'run')
''' % (str(ROOT), str(ROOT / 'FusionBulkExport' / 'FusionBulkExport.py'))
        # A bare environment, but Windows' Python cannot start without SYSTEMROOT (3.9 then fails
        # with "failed to get random numbers to initialize Python").
        env = {'PATH': os.defpath}
        if os.environ.get('SYSTEMROOT'):
            env['SYSTEMROOT'] = os.environ['SYSTEMROOT']
        result = subprocess.run([sys.executable, '-c', script], cwd=os.path.abspath(os.sep), capture_output=True,
                                text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
