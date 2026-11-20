"""Runs the tree page's logic test with node when node is installed."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class TreeLogicTests(unittest.TestCase):
    def test_tree_logic(self):
        result = subprocess.run(['node', str(ROOT / 'tests' / 'tree_logic_test.js')],
                                capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        for part in ('tree logic ok', 'errors and retry ok', 'keyboard ok', 'page wiring ok'):
            self.assertIn(part, result.stdout)
