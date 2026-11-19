"""Unattended export: configuration, inventory, project selection, limit, stop."""
import importlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from tests import fake_adsk as fake

fake.install()
from fusion_bulk_export.autorun import run_autorun


class AutorunTests(unittest.TestCase):
    def setUp(self):
        import adsk
        adsk.doEvents = lambda: None
        adsk.core.CommandEventHandler = object
        adsk.core.CommandCreatedEventHandler = object
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'out'
        self.config = self.root / 'autorun.json'
        self.files = [fake.File('A', 'a'), fake.File('B', 'b'), fake.File('Drawing', 'd', extension='f2d')]
        self.data = fake.data(self.files[:2], [fake.folder('Sub', files=self.files[2:])])
        other = fake.File('C', 'c')
        hub = self.data.dataHubs.item(0)
        hub.dataProjects.items.append(NS(id='project-2', name='Two', rootFolder=fake.folder(files=[other])))
        self.files.append(other)
        self.app = NS(version='test-version', documents=fake.Documents(), data=self.data, userInterface=NS())
        adsk.core.Application = NS(get=lambda: self.app)
        sleep = patch('fusion_bulk_export.autorun.time.sleep')
        sleep.start()
        self.addCleanup(sleep.stop)

    def run_with(self, **settings):
        settings.setdefault('output', str(self.output))
        self.config.write_text(json.dumps(settings))
        result = run_autorun(self.app, self.config)
        self.assertEqual(result, json.loads((Path(settings['output']) / 'autorun-result.json').read_text()))
        return result

    def test_export_everything_and_consume_config(self):
        result = self.run_with()
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['counts'], dict(done=3, failed=0, skipped=1))
        self.assertEqual(result['found'], 4)
        self.assertGreater(result['bytes'], 0)
        self.assertFalse(self.config.exists())
        self.assertTrue((self.root / 'autorun.json.last').exists())
        self.assertIn('design 4/4', (self.output / 'autorun.log').read_text())

    def test_inventory_opens_nothing(self):
        result = self.run_with(mode='inventory')
        self.assertEqual(result['counts'], dict(hubs=1, projects=2, files=4, designs=3, unreadable=0))
        listing = json.loads((self.output / 'inventory.json').read_text())
        self.assertEqual(len(listing['hubs']['hub-1']['projects']['project-1']['files']), 3)
        self.assertTrue(all(file.calls == 0 for file in self.files))

    def test_selected_project_and_limit(self):
        result = self.run_with(projects=['project-1'], limit=1)
        self.assertEqual(result['projects_matched'], 1)
        self.assertEqual(result['counts']['done'], 1)
        self.assertEqual(self.files[3].calls, 0)

    def test_unknown_project_is_an_error(self):
        result = self.run_with(projects=['missing'])
        self.assertIn('requested projects', result['errors'][0]['error'])

    def test_stop_file_pauses_and_rerun_resumes(self):
        self.output.mkdir()
        (self.output / 'STOP').write_text('')
        result = self.run_with()
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['counts']['done'], 0)
        (self.output / 'STOP').unlink()
        self.assertEqual(self.run_with()['counts']['done'], 3)
        self.assertEqual(self.run_with()['counts'], dict(done=0, failed=0, skipped=4))

    def test_unreadable_project_is_recorded_and_others_continue(self):
        class Failing:
            id, name = 'project-3', 'Broken'

            @property
            def rootFolder(self):
                raise RuntimeError('No access')
        self.data.dataHubs.item(0).dataProjects.items.append(Failing())
        result = self.run_with()
        self.assertEqual(result['counts']['done'], 3)
        self.assertEqual(result['discovery_errors'], 1)
        self.assertIn('No access', (self.output / 'export.log').read_text())

    def test_no_hubs_times_out(self):
        self.data.dataHubs.items.clear()
        with patch('fusion_bulk_export.autorun.time.monotonic', side_effect=range(0, 100000, 1000)):
            result = self.run_with()
        self.assertIn('signed in', result['errors'][0]['error'])

    def test_bad_config_reports_beside_add_in(self):
        self.config.write_text('{bad')
        result = run_autorun(self.app, self.config)
        self.assertIn('JSONDecodeError', result['errors'][0]['traceback'])
        self.assertTrue((self.root / 'autorun-output' / 'autorun-result.json').exists())

    def test_startup_dispatches_autorun_before_ui(self):
        self.config.write_text(json.dumps(dict(output=str(self.output))))
        module = importlib.import_module('FusionBulkExport')
        with patch.object(module, '__file__', str(self.root / 'FusionBulkExport.py')):
            module.run(None)
        self.assertEqual(json.loads((self.output / 'autorun-result.json').read_text())['counts']['done'], 3)


if __name__ == '__main__':
    unittest.main()
