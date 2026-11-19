"""Unattended lifecycle, asynchronous upload and evidence checks."""
import importlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from tests import fake_adsk as fake

fake.install()
from fusion_bulk_export.selftest import run_selftest


class SelfTestTests(unittest.TestCase):
    def setUp(self):
        import adsk
        self.adsk = adsk
        adsk.core.UploadStates = NS(UploadProcessing=0, UploadFinished=1, UploadFailed=2)
        adsk.doEvents = lambda: None
        adsk.core.CommandEventHandler = object
        adsk.core.CommandCreatedEventHandler = object
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.seed = self.root / 'seeds'
        self.seed.mkdir()
        (self.seed / 'Part.f3d').write_bytes(b'fake')
        nested = self.seed / 'nested'
        nested.mkdir()
        (nested / 'Assembly.F3Z').write_bytes(b'fake')
        (self.seed / 'ignored.step').write_bytes(b'fake')
        self.output = self.root / 'out'
        self.config = self.root / 'selftest.json'
        self.settings = dict(output=str(self.output), seed_folder=str(self.seed), project='bulk-export-selftest')
        self.config.write_text(json.dumps(self.settings))
        self.projects = fake.Projects()
        self.hub = NS(id='first-hub', name='First', dataProjects=self.projects)
        self.app = NS(version='test-version', documents=fake.Documents(),
                      data=NS(dataHubs=fake.Collection(self.hub)))
        adsk.core.Application = NS(get=lambda: self.app)
        # Accessing any UI method is an error: self-test must never ask for input.
        self.app.userInterface = NS()
        self.sleep = patch('fusion_bulk_export.selftest.time.sleep')
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def run_test(self):
        result = run_selftest(self.app, self.config)
        self.assertEqual(result, json.loads((self.output / 'selftest-result.json').read_text()))
        return result

    def test_creates_project_polls_uploads_and_exports_real_pipeline(self):
        result = self.run_test()
        self.assertTrue(result['passed'], result['errors'])
        self.assertEqual(result['fusion_version'], 'test-version')
        self.assertEqual(self.projects.created, ['bulk-export-selftest'])
        folder = self.projects.item(0).rootFolder
        self.assertEqual(len(folder.uploads), 2)
        self.assertTrue(all(future.polls == 2 for future in folder.futures))
        self.assertEqual(folder.processing_polls, 4)
        self.assertEqual([u['status'] for u in result['uploads']], ['done', 'done'])
        self.assertEqual(sorted(d['child_references'] for d in result['designs']), [0, 1])
        for design in result['designs']:
            self.assertTrue(design['archive_correct'])
            self.assertEqual(design['solid_bodies'], 2)
            self.assertGreaterEqual(design['attempt_details'][0]['seconds'], 0)
            for file in design['files']:
                self.assertTrue((self.output / file['path']).exists())
        self.assertIn('upload start', (self.output / 'selftest.log').read_text())
        self.assertIn('export end', (self.output / 'selftest.log').read_text())
        self.assertTrue(all(doc.closed for file in folder.dataFiles.items for doc in file.documents))

    def add_unrelated_designs(self):
        other_project = fake.File(identity='other-project-file')
        self.projects.items.append(NS(id='other-project', name='Other', rootFolder=fake.folder(files=[other_project])))
        other_file = fake.File(identity='other-file')
        self.app.data.dataHubs.items.append(fake.data([other_file]).dataHubs.item(0))
        return other_project, other_file

    def test_reuses_named_project_and_exports_only_it_by_default(self):
        project = self.projects.add('bulk-export-selftest')
        self.projects.created.clear()
        unrelated = self.add_unrelated_designs()
        result = self.run_test()
        self.assertTrue(result['passed'], result['errors'])
        self.assertEqual(result['scope'], 'project')
        self.assertEqual(self.projects.created, [])
        self.assertEqual(len(project.rootFolder.uploads), 2)
        self.assertEqual(result['counts']['done'], 2)
        # Designs outside the self-test project are never opened.
        self.assertTrue(all(file.calls == 0 for file in unrelated))

    def test_scope_all_exports_every_hub(self):
        self.projects.add('bulk-export-selftest')
        self.add_unrelated_designs()
        self.settings['scope'] = 'all'
        self.config.write_text(json.dumps(self.settings))
        result = self.run_test()
        self.assertTrue(result['passed'], result['errors'])
        self.assertEqual(result['counts']['done'], 4)

    def test_invalid_scope_is_reported(self):
        self.settings['scope'] = 'everything'
        self.config.write_text(json.dumps(self.settings))
        result = self.run_test()
        self.assertFalse(result['passed'])
        self.assertIn('scope', result['errors'][0]['error'])

    def test_configuration_is_consumed_once(self):
        self.run_test()
        self.assertFalse(self.config.exists())
        self.assertTrue((self.root / 'selftest.json.last').is_file())

    def test_unrecorded_references_fail_the_check(self):
        with patch('fusion_bulk_export.fusion.FusionAdapter.references', return_value=[]):
            result = self.run_test()
        self.assertFalse(result['passed'])
        assembly = next(d for d in result['designs'] if d['name'] == 'Assembly')
        self.assertEqual(assembly['child_references'], 1)
        self.assertFalse(assembly['archive_correct'])

    def test_destination_errors_include_tracebacks_for_both_attempts(self):
        with patch('fusion_bulk_export.core.Exporter.destination', side_effect=OSError('No destination')):
            result = self.run_test()
        self.assertFalse(result['passed'])
        self.assertEqual(len(result['errors']), 5)  # Four attempts and missing-upload verification.
        for design in result['designs']:
            self.assertEqual(design['status'], 'failed')
            self.assertEqual(len(design['errors']), 2)
            self.assertIn('OSError: No destination', design['errors'][0]['traceback'])

    def test_upload_failure_keeps_error_and_continues(self):
        project = self.projects.add('bulk-export-selftest')
        project.rootFolder.next_states = (2,)
        result = self.run_test()
        self.assertFalse(result['passed'])
        self.assertEqual(len(result['uploads']), 2)
        self.assertTrue(all(u['status'] == 'failed' for u in result['uploads']))
        self.assertIn('RuntimeError', result['errors'][0]['traceback'])
        self.assertIn('Fusion upload failed', (self.output / 'selftest.log').read_text())

    def test_upload_timeout_is_reported(self):
        project = self.projects.add('bulk-export-selftest')
        project.rootFolder.next_states = (0,)
        with patch('fusion_bulk_export.selftest.time.monotonic', side_effect=range(0, 100000, 1000)):
            result = self.run_test()
        self.assertFalse(result['passed'])
        self.assertTrue(any('Timed out waiting for upload' in e['error'] for e in result['errors']))

    def test_processing_timeout_is_reported(self):
        project = self.projects.add('bulk-export-selftest')
        project.rootFolder.next_states = (1,)
        original = project.rootFolder.uploadFile

        def upload(path):
            future = original(path)
            future.dataFile.refresh = lambda handler: True
            return future

        project.rootFolder.uploadFile = upload
        with patch('fusion_bulk_export.selftest.time.monotonic', side_effect=range(0, 100000, 1000)):
            result = self.run_test()
        self.assertFalse(result['passed'])
        self.assertTrue(any('cloud processing' in e['error'] for e in result['errors']))

    def test_export_retry_tracebacks_are_preserved(self):
        project = self.projects.add('bulk-export-selftest')
        existing = fake.File(identity='failing')
        existing.failures = 1
        project.rootFolder.dataFiles.items.append(existing)
        result = self.run_test()
        self.assertFalse(result['passed'])
        entry = next(d for d in result['designs'] if d['id'] == 'failing')
        self.assertEqual(entry['status'], 'done')
        self.assertEqual(len(entry['attempt_details']), 2)
        self.assertIn('Synthetic export failure', entry['attempt_details'][0]['traceback'])
        self.assertEqual(len(result['errors']), 1)

    def test_missing_uploaded_design_is_failure(self):
        project = self.projects.add('bulk-export-selftest')
        upload = project.rootFolder.uploadFile

        def hidden(path):
            future = upload(path)
            project.rootFolder.dataFiles.items.clear()
            return future

        project.rootFolder.uploadFile = hidden
        result = self.run_test()
        self.assertFalse(result['passed'])
        self.assertIn('not exported', result['errors'][-1]['error'])

    def test_no_hub_timeout_and_invalid_seed_write_results(self):
        self.app.data.dataHubs.items.clear()
        with patch('fusion_bulk_export.selftest.time.monotonic', side_effect=range(0, 100000, 1000)):
            result = self.run_test()
        self.assertIn('first hub', result['errors'][0]['error'])
        self.settings['seed_folder'] = str(self.root / 'missing')
        self.config.write_text(json.dumps(self.settings))
        result = self.run_test()
        self.assertFalse(result['passed'])
        self.assertIn('seed_folder', result['errors'][0]['error'])

    def test_bad_config_has_fallback_diagnostics_without_ui(self):
        self.config.write_text('{bad')
        result = run_selftest(self.app, self.config)
        self.assertFalse(result['passed'])
        self.assertTrue((self.root / 'selftest-output' / 'selftest-result.json').exists())
        self.assertIn('JSONDecodeError', result['errors'][0]['traceback'])

    def test_startup_dispatches_without_ui(self):
        module = importlib.import_module('FusionBulkExport')
        # Pretend the installed entry point lives alongside our configuration.
        with patch.object(module, '__file__', str(self.root / 'FusionBulkExport.py')):
            module.run(None)
        self.assertTrue(json.loads((self.output / 'selftest-result.json').read_text())['passed'])
