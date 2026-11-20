"""Remembered dialog settings: where they live, reading and merging, folder facts."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import fake_adsk as fake

fake.install()
from fusion_bulk_export import settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_app_data_folder_per_system_never_beside_the_add_in(self):
        mac = settings.settings_path('darwin', {}, home='/Users/u')
        self.assertEqual(mac.as_posix(), '/Users/u/Library/Application Support/FusionBulkExport/settings.json')
        win = settings.settings_path('win32', {'APPDATA': '/appdata'}, home='/h')
        self.assertEqual(win.as_posix(), '/appdata/FusionBulkExport/settings.json')
        self.assertEqual(settings.settings_path('darwin', {'FUSION_BULK_EXPORT_SETTINGS': '/t/s.json'}).as_posix(),
                         '/t/s.json')
        add_in = Path(settings.__file__).resolve().parents[1]
        self.assertNotIn(str(add_in), str(settings.settings_path('darwin', {})))

    def test_load_and_store_merge(self):
        path = self.root / 'deep' / 'settings.json'
        self.assertEqual(settings.load(path), {})
        self.assertTrue(settings.store(dict(output='/a'), path))
        self.assertTrue(settings.store(dict(mode='Everything'), path))
        self.assertEqual(settings.load(path), dict(output='/a', mode='Everything'))
        path.write_text('[1, 2')
        self.assertEqual(settings.load(path), {})

    def test_store_failure_is_quiet(self):
        blocker = self.root / 'file'
        blocker.write_text('')
        self.assertFalse(settings.store(dict(output='/a'), blocker / 'settings.json'))

    def test_folder_facts(self):
        self.assertEqual(settings.folder_facts(str(self.root / 'none'))['state'], 'new')
        self.assertEqual(settings.folder_facts(str(self.root))['state'], 'empty')
        (self.root / 'manifest.json').write_text('{oops')
        facts = settings.folder_facts(str(self.root))
        self.assertEqual(facts['state'], 'unreadable')
        self.assertIn('cannot be read', settings.folder_note(facts))
        (self.root / 'manifest.json').write_text(json.dumps(dict(entries=[
            dict(key=json.dumps(['h', 'p1', 'f', 1]), status='done'),
            dict(key='not json', status='done')])))
        facts = settings.folder_facts(str(self.root))
        self.assertEqual((facts['state'], facts['done'], facts['projects']), ('continues', 2, {'p1': 1}))

    def test_default_output(self):
        self.assertEqual(Path(settings.default_output('/home/u')).as_posix(), '/home/u/Documents/FusionExport')


if __name__ == '__main__':
    unittest.main()
