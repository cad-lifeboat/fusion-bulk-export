"""Synthetic command lifecycle and output-folder dialog checks."""
import importlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from tests import fake_adsk as fake

fake.install()


class Event:
    def __init__(self):
        self.handlers = []

    def add(self, handler):
        self.handlers.append(handler)


class UIBase(unittest.TestCase):
    def setUp(self):
        import adsk
        self.adsk = adsk
        adsk.core.CommandEventHandler = object
        adsk.core.CommandCreatedEventHandler = object
        self.temp = tempfile.TemporaryDirectory(prefix='fusion-')
        self.addCleanup(self.temp.cleanup)
        self.folder = NS(title='', folder=self.temp.name, showDialog=lambda: adsk.core.DialogResults.DialogOK)
        # Remembered settings live in a temporary file here; the last folder is the test's folder.
        home = tempfile.TemporaryDirectory(prefix='home-')
        self.addCleanup(home.cleanup)
        self.settings = Path(home.name) / 'settings.json'
        self.settings.write_text(json.dumps(dict(output=self.temp.name)))
        environment = patch.dict('os.environ', FUSION_BULK_EXPORT_SETTINGS=str(self.settings))
        environment.start()
        self.addCleanup(environment.stop)
        self.progress = NS(isCancelButtonShown=False, wasCancelled=False,
                           show=lambda *args: None, hide=lambda: None)
        self.summaries, self.errors, self.details, self.troubles, self.opened = [], [], [], [], []
        self.answer = adsk.core.DialogResults.DialogCancel  # to the end-of-run dialog
        self.trouble_answer = adsk.core.DialogResults.DialogCancel  # to carry on (OK) / stop
        buttons = adsk.core.MessageBoxButtonTypes

        def message_box(*args):
            if len(args) > 2 and args[2] == buttons.YesNoCancelButtonType:
                self.summaries.append(args)
                return self.answer
            if len(args) > 2 and args[2] == buttons.OKCancelButtonType:
                self.troubles.append(args)
                return self.trouble_answer
            self.errors.append(args)
            return adsk.core.DialogResults.DialogOK
        self.definition = NS(commandCreated=Event(), deleteMe=lambda: None)
        self.control = NS(deleteMe=lambda: None)
        self.definitions = NS(addButtonDefinition=lambda *args: self.definition)
        self.ui = NS(createFolderDialog=lambda: self.folder,
                     createProgressDialog=lambda: self.progress,
                     inputBox=lambda *args: self.details.append(args),
                     messageBox=message_box,
                     commandDefinitions=self.definitions,
                     allToolbarPanels=NS(itemById=lambda name: NS(controls=NS(addCommand=lambda definition: self.control))))
        self.file = fake.File()
        self.events = {}
        self.app = NS(userInterface=self.ui, documents=fake.Documents(), data=fake.data([self.file]),
                      registerCustomEvent=lambda name: self.events.setdefault(name, Event()),
                      unregisterCustomEvent=lambda name: self.events.pop(name, None),
                      fireCustomEvent=lambda name, info: None)
        adsk.core.Application = NS(get=lambda: self.app)
        adsk.doEvents = lambda: None
        self.module = importlib.import_module('FusionBulkExport')
        # The add-in's own folder: job files and the heartbeat go to a temporary one here.
        addin = tempfile.TemporaryDirectory(prefix='addin-')
        self.addCleanup(addin.cleanup)
        self.addin = Path(addin.name)
        for target, value in ((self.module, '_HERE'), (self.module.Watcher, 'start')):
            patcher = patch.object(target, value, self.addin if value == '_HERE' else (lambda watcher: None))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.module.stop, None)
        opener = patch('fusion_bulk_export.report.open_path', side_effect=self.opened.append)
        opener.start()
        self.addCleanup(opener.stop)

    def open_dialog(self):
        self.module.run(None) if not self.definition.commandCreated.handlers else None
        command = NS(execute=Event(), inputChanged=Event(), validateInputs=Event(), incomingFromHTML=Event(),
                     commandInputs=fake.Inputs(),
                     setDialogInitialSize=lambda w, h: None, setDialogMinimumSize=lambda w, h: None)
        self.definition.commandCreated.handlers[-1].notify(NS(command=command))
        self.assertEqual(self.errors, [])
        return command

    def change(self, command, control):
        command.inputChanged.handlers[0].notify(NS(input=control))
        self.assertEqual(self.errors, [])

    def picker_data(self):
        """Hub with two projects; the first has folders Top/Sub and Other."""
        self.files = {i: fake.File(identity=i) for i in ('root', 'top', 'deep', 'other', 'second')}
        sub = fake.folder('Sub', files=[self.files['deep']])
        self.app.data = fake.data([self.files['root']], [fake.folder('Top', files=[self.files['top']], folders=[sub]),
                                                          fake.folder('Other', files=[self.files['other']])])
        self.app.data.dataHubs.item(0).dataProjects.items.append(
            NS(id='project-2', name='Two', rootFolder=fake.folder(files=[self.files['second']])))

    def exported(self):
        manifest = json.loads((Path(self.temp.name) / 'manifest.json').read_text())
        return sorted(e['id'] for e in manifest['entries'] if e['status'] == 'done')

    def page(self, command, action, data=None):
        """What the tree page does: send a request and read the reply."""
        args = NS(action=action, data=json.dumps(data or {}), returnData='')
        command.incomingFromHTML.handlers[0].notify(args)
        self.assertEqual(self.errors, [])
        return json.loads(args.returnData)

    def choose(self, command):
        inputs = command.commandInputs
        inputs.find('fbx_mode').select('Choose projects or folders')
        self.change(command, inputs.find('fbx_mode'))
        return inputs

    def valid(self, command):
        args = NS(areInputsValid=True)
        command.validateInputs.handlers[0].notify(args)
        return args.areInputsValid

class UItests(UIBase):
    def test_dialog_defaults_to_everything(self):
        self.picker_data()
        command = self.open_dialog()
        inputs = command.commandInputs
        self.assertEqual(inputs.find('fbx_mode').selectedItem.name, 'Everything')
        self.assertFalse(inputs.find('fbx_tree').isVisible)
        self.assertIn('one project or folder', inputs.find('fbx_tip').text)
        self.assertTrue(inputs.find('fbx_tree').htmlFileURL.endswith('/ui/tree.html'))
        self.assertTrue(self.valid(command))
        command.execute.handlers[0].notify(None)
        self.assertEqual(self.exported(), ['deep', 'other', 'root', 'second', 'top'])

    def test_dialog_shows_the_version(self):
        manifest = json.loads((Path(__file__).resolve().parents[1] / 'FusionBulkExport' /
                               'FusionBulkExport.manifest').read_text())
        inputs = self.open_dialog().commandInputs
        self.assertEqual(inputs.find('fbx_version').text, 'Fusion Bulk Export ' + manifest['version'])
        from fusion_bulk_export.picker import version
        self.assertIsNone(version(Path(self.temp.name) / 'missing.manifest'))

    def test_tree_lists_hubs_projects_and_folders_on_demand(self):
        self.picker_data()
        command = self.open_dialog()
        inputs = self.choose(command)
        self.assertTrue(inputs.find('fbx_tree').isVisible)
        hubs = self.page(command, 'hubs')['hubs']
        self.assertEqual([(p['id'], p['name']) for p in hubs[0]['projects']],
                         [('project-1', 'Project'), ('project-2', 'Two')])
        reply = self.page(command, 'folders', dict(project='project-1', path=[]))
        self.assertEqual(reply, dict(folders=[dict(id=None, name='Top'), dict(id=None, name='Other')], files=1))
        self.assertEqual(self.page(command, 'folders', dict(project='project-1', path=['Top']))['folders'],
                         [dict(id=None, name='Sub')])

    def test_failing_folder_request_is_an_error_not_no_folders(self):
        self.picker_data()
        command = self.open_dialog()
        self.choose(command)
        self.page(command, 'hubs')
        top = self.app.data.dataHubs.item(0).dataProjects.item(0).rootFolder.dataFolders.item(0)
        top.dataFolders = None  # listing fails
        reply = self.page(command, 'folders', dict(project='project-1', path=['Top']))
        self.assertIn('error', reply)
        self.assertNotIn('folders', reply)
        reply = self.page(command, 'folders', dict(project='project-1', path=['Gone']))
        self.assertIn('no longer there', reply['error'])

    def test_same_named_folders_are_addressed_by_id(self):
        first = fake.folder('Twin', folders=[fake.folder('Inside one')], identity='folder-a')
        second = fake.folder('Twin', folders=[fake.folder('Inside two')], identity='folder-b')
        self.app.data = fake.data([], [first, second])
        command = self.open_dialog()
        self.choose(command)
        self.page(command, 'hubs')
        reply = self.page(command, 'folders', dict(project='project-1', path=[]))
        self.assertEqual(reply['folders'], [dict(id='folder-a', name='Twin'), dict(id='folder-b', name='Twin')])
        reply = self.page(command, 'folders', dict(project='project-1', path=['Twin'], ids=['folder-b']))
        self.assertEqual(reply['folders'], [dict(id=None, name='Inside two')])

    def test_ticked_twin_exports_only_that_folder(self):
        one, two = fake.File('One', 'one'), fake.File('Two', 'two')
        self.app.data = fake.data([], [fake.folder('Twin', [one], identity='folder-a'),
                                       fake.folder('Twin', [two], identity='folder-b')])
        command = self.open_dialog()
        self.choose(command)
        self.page(command, 'hubs')
        self.page(command, 'selection', {'project-1': [[{'id': 'folder-b', 'name': 'Twin'}]]})
        command.execute.handlers[0].notify(None)
        self.assertEqual(self.exported(), ['two'])
        self.assertEqual(one.calls, 0)

    def test_projects_already_exported_are_marked(self):
        self.picker_data()
        self.open_dialog().execute.handlers[0].notify(None)  # everything into the folder
        command = self.open_dialog()
        self.choose(command)
        hubs = self.page(command, 'hubs')['hubs']
        self.assertEqual([p['exported'] for p in hubs[0]['projects']], [4, 1])
        # Browsing to an empty folder tells the page the marks are gone.
        empty = Path(self.temp.name) / 'empty'
        empty.mkdir()
        self.folder.folder = str(empty)
        self.change(command, command.commandInputs.find('fbx_browse'))
        self.assertEqual(command.commandInputs.find('fbx_tree').sent, [('exported', '{}')])

    def test_ticked_folder_exports_its_subtree(self):
        self.picker_data()
        command = self.open_dialog()
        inputs = self.choose(command)
        self.page(command, 'hubs')
        self.assertFalse(self.valid(command))
        self.assertEqual(inputs.find('fbx_summary').text, 'Nothing chosen yet.')
        self.page(command, 'selection', {'project-1': [['Top']]})
        self.assertEqual(inputs.find('fbx_summary').text, '0 whole project(s) and 1 folder(s) chosen.')
        self.assertTrue(self.valid(command))
        command.execute.handlers[0].notify(None)
        self.assertEqual(self.exported(), ['deep', 'top'])

    def test_ticked_project_exports_whole_project(self):
        self.picker_data()
        command = self.open_dialog()
        self.choose(command)
        self.page(command, 'hubs')
        self.page(command, 'selection', {'project-2': None})
        command.execute.handlers[0].notify(None)
        self.assertEqual(self.exported(), ['second'])

    def test_unticking_everything_disables_export(self):
        self.picker_data()
        command = self.open_dialog()
        self.choose(command)
        self.page(command, 'hubs')
        self.page(command, 'selection', {'project-2': None})
        self.page(command, 'selection', {})
        self.assertFalse(self.valid(command))
        command.execute.handlers[0].notify(None)
        self.assertIn('Tick at least one', self.errors[0][0])
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_lifecycle_summary_and_report(self):
        self.module.run(None)
        self.assertEqual(self.errors, [])
        command = self.open_dialog()
        self.answer = self.adsk.core.DialogResults.DialogYes
        command.execute.handlers[0].notify(None)
        self.assertEqual(self.errors, [])
        self.assertIsNone(self.module._busy['by'])  # free for command-line jobs again
        self.assertEqual(len(self.summaries), 1)
        message, title = self.summaries[0][:2]
        self.assertIn('1 exported now, 0 already up to date, 0 other file(s) saved, 0 skipped, 0 failed.', message)
        self.assertIn(self.temp.name, message)
        self.assertNotIn('Run again', message)
        self.assertEqual(title, 'Fusion Bulk Export')
        folder = Path(self.temp.name)
        self.assertTrue((folder / 'manifest.json').exists())
        self.assertEqual(self.opened, [folder.resolve() / 'index.html'])
        self.assertTrue((folder / 'index.csv').exists())
        # A second run: nothing new, all up to date; No opens the folder.
        self.answer = self.adsk.core.DialogResults.DialogNo
        self.open_dialog().execute.handlers[0].notify(None)
        self.assertIn('0 exported now, 1 already up to date', self.summaries[1][0])
        self.assertEqual(str(self.opened[1]), self.temp.name)

    def test_failure_reasons_and_retry_hint(self):
        self.file.failures = 2  # both attempts fail
        self.open_dialog().execute.handlers[0].notify(None)
        message = self.summaries[0][0]
        self.assertIn('0 exported now, 0 already up to date, 0 other file(s) saved, 0 skipped, 1 failed.', message)
        self.assertIn('- Part: Synthetic export failure', message)
        self.assertIn('Run again to retry the 1 failed.', message)
        page = (Path(self.temp.name) / 'index.html').read_text()
        self.assertIn('Synthetic export failure', page)
        self.assertEqual(self.opened, [])  # Cancel opens nothing

    def test_empty_step_is_a_warning_in_the_summary(self):
        self.file.step_solids = 0
        self.open_dialog().execute.handlers[0].notify(None)
        message = self.summaries[0][0]
        self.assertIn('1 exported now', message)
        self.assertIn('1 exported with a warning', message)
        self.assertIn('- Part: STEP has no bodies (Fusion showed 1)', message)

    def test_trouble_asks_retry_or_stop(self):
        files = [fake.File('P%d' % n, 'f%d' % n) for n in range(7)]
        for file in files:
            file.failures = 2
        self.app.data = fake.data(files)
        self.open_dialog().execute.handlers[0].notify(None)
        self.assertEqual(len(self.troubles), 1)
        self.assertIn('in a row failed', self.troubles[0][0])
        message = self.summaries[0][0]
        self.assertIn('The export stopped: 5 designs in a row failed', message)
        self.assertIn('signed in', message)
        self.assertEqual(sum(file.calls for file in files), 10)

    def test_trouble_retry_carries_on(self):
        files = [fake.File('P%d' % n, 'f%d' % n) for n in range(7)]
        for file in files[:5]:
            file.failures = 2
        self.app.data = fake.data(files)
        self.trouble_answer = self.adsk.core.DialogResults.DialogOK
        self.open_dialog().execute.handlers[0].notify(None)
        self.assertEqual(len(self.troubles), 1)
        self.assertIn('2 exported now', self.summaries[0][0])
        self.assertIn('Run again to retry the 5 failed.', self.summaries[0][0])

    def test_dialogs_work_without_optional_enum_members(self):
        # Icons are optional, and no Retry button or DialogRetry result is used.
        self.assertFalse(hasattr(self.adsk.core.DialogResults, 'DialogRetry'))
        self.assertFalse(hasattr(self.adsk.core.MessageBoxButtonTypes, 'RetryCancelButtonType'))
        files = [fake.File('P%d' % n, 'f%d' % n) for n in range(7)]
        for file in files[:5]:
            file.failures = 2
        self.app.data = fake.data(files)
        self.trouble_answer = self.adsk.core.DialogResults.DialogOK
        with patch.object(self.adsk.core, 'MessageBoxIconTypes', NS()):
            self.open_dialog().execute.handlers[0].notify(None)
        self.assertEqual([len(args) for args in self.troubles + self.summaries], [3, 3])
        self.assertIn('2 exported now', self.summaries[0][0])

    def test_low_space_is_reported(self):
        with patch('fusion_bulk_export.core.free_bytes', return_value=200 << 20):
            self.open_dialog().execute.handlers[0].notify(None)
        self.assertIn('Low disk space: 200 MB free.', self.summaries[0][0])

    def test_browse_cancel_keeps_folder_and_tree(self):
        self.picker_data()
        command = self.open_dialog()
        inputs = self.choose(command)
        self.page(command, 'hubs')
        self.page(command, 'selection', {'project-2': None})
        self.folder.showDialog = lambda: self.adsk.core.DialogResults.DialogCancel
        self.change(command, inputs.find('fbx_browse'))
        self.assertEqual(inputs.find('fbx_folder').text, self.temp.name)
        self.assertTrue(self.valid(command))
        self.assertEqual(self.file.calls, 0)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_browse_sets_folder_and_says_it_continues(self):
        other = Path(self.temp.name) / 'other'
        other.mkdir()
        (other / 'manifest.json').write_text(json.dumps(dict(schema=2, runs=[], entries=[
            dict(key=json.dumps(['hub-1', 'project-1', 'x', 1]), status='done'),
            dict(key=json.dumps(['hub-1', 'project-1', 'y', 1]), status='failed')])))
        (other / 'manifest.journal').write_text(json.dumps(dict(entry=dict(
            key=json.dumps(['hub-1', 'project-2', 'z', 1]), status='done'))) + '\n')
        command = self.open_dialog()
        inputs = command.commandInputs
        self.assertEqual(inputs.find('fbx_note').text, 'A new export into this folder.')
        self.folder.folder = other.as_posix()
        self.change(command, inputs.find('fbx_browse'))
        self.assertEqual(inputs.find('fbx_folder').text, str(other))
        self.assertEqual(inputs.find('fbx_note').text, 'Continues the export in this folder (2 done).')

    def test_folder_and_mode_are_remembered(self):
        self.picker_data()
        command = self.open_dialog()
        self.choose(command)
        self.page(command, 'hubs')
        self.page(command, 'selection', {'project-2': None})
        command.execute.handlers[0].notify(None)
        self.assertEqual(json.loads(self.settings.read_text()),
                         dict(output=self.temp.name, mode='Choose projects or folders', other_files=True))
        inputs = self.open_dialog().commandInputs
        self.assertEqual(inputs.find('fbx_mode').selectedItem.name, 'Choose projects or folders')
        self.assertTrue(inputs.find('fbx_tree').isVisible)
        self.assertEqual(inputs.find('fbx_folder').text, self.temp.name)

    def test_other_files_checkbox_is_on_by_default_and_remembered(self):
        spec = fake.File('Spec', 'spec', extension='pdf')
        self.app.data = fake.data([self.file, spec])
        command = self.open_dialog()
        box = command.commandInputs.find('fbx_files')
        self.assertEqual(box.name, 'Also save other files (PDFs, drawings, uploads)')
        self.assertTrue(box.value)
        command.execute.handlers[0].notify(None)
        self.assertIn('1 exported now, 0 already up to date, 1 other file(s) saved', self.summaries[0][0])
        self.assertTrue((Path(self.temp.name) / 'Hub' / 'Project' / 'Spec.pdf').is_file())
        command = self.open_dialog()
        command.commandInputs.find('fbx_files').value = False
        spec.versionNumber = 4
        command.execute.handlers[0].notify(None)
        self.assertEqual(json.loads(self.settings.read_text())['other_files'], False)
        self.assertEqual(len(spec.downloads), 1)  # not saved with the box off
        self.assertIn('1 already up to date, 0 other file(s) saved, 1 skipped', self.summaries[1][0])
        self.assertFalse(self.open_dialog().commandInputs.find('fbx_files').value)

    def test_first_use_suggests_documents_folder(self):
        self.settings.unlink()
        with patch('fusion_bulk_export.settings.Path.home', return_value=Path(self.temp.name) / 'home'):
            command = self.open_dialog()
        inputs = command.commandInputs
        suggested = Path(self.temp.name) / 'home' / 'Documents' / 'FusionExport'
        self.assertEqual(inputs.find('fbx_folder').text, str(suggested))
        self.assertIn('created when the export starts', inputs.find('fbx_note').text)
        self.assertFalse(suggested.exists())
        self.assertTrue(self.valid(command))
        command.execute.handlers[0].notify(None)
        self.assertTrue((suggested / 'manifest.json').is_file())

    def test_no_folder_disables_export(self):
        command = self.open_dialog()
        self.module._picker.output = ''
        self.assertFalse(self.valid(command))

    def test_progress_cancel_keeps_manifest(self):
        self.progress.wasCancelled = True
        self.open_dialog().execute.handlers[0].notify(None)
        self.assertEqual(self.file.calls, 0)
        self.assertIn('Export cancelled', self.summaries[0][0])
        self.assertTrue((Path(self.temp.name) / 'manifest.json').exists())


class FriendlyErrorTests(UIBase):
    def setUp(self):
        super().setUp()
        log = tempfile.TemporaryDirectory(prefix='log-')
        self.addCleanup(log.cleanup)
        self.log = Path(log.name)
        patcher = patch.object(self.module, '_LOG_DIR', str(self.log))
        patcher.start()
        self.addCleanup(patcher.stop)

    def export(self):
        command = self.open_dialog()
        self.errors.clear()
        command.execute.handlers[0].notify(None)
        self.assertEqual(len(self.errors), 1, self.errors)
        text = self.errors[0][0]
        self.assertNotIn('Traceback', text)
        self.assertFalse((self.log / 'errors.log').exists())
        return text

    def test_unusable_folder_is_one_plain_sentence(self):
        target = Path(self.temp.name) / 'a file'
        target.write_text('')
        self.settings.write_text(json.dumps(dict(output=str(target))))
        text = self.export()
        self.assertTrue(text.startswith('This folder cannot be used for the export: '), text)
        self.assertIn('Choose a folder', text)
        self.assertEqual(self.summaries, [])

    def test_damaged_manifest(self):
        (Path(self.temp.name) / 'manifest.json').write_text('{not json')
        text = self.export()
        self.assertIn('manifest.json in', text)
        self.assertIn('cannot be read', text)
        self.assertEqual(self.file.calls, 0)

    def test_newer_manifest(self):
        (Path(self.temp.name) / 'manifest.json').write_text('{"schema": 99, "entries": [], "runs": []}')
        self.assertIn('newer version', self.export())

    def test_signed_out(self):
        self.app.data.dataHubs.items.clear()
        text = self.export()
        self.assertIn('no hubs', text)
        self.assertIn('Sign in', text)

    def test_unexpected_error_is_logged_with_copyable_details(self):
        command = self.open_dialog()
        self.module._code['export'] = lambda *args, **kwargs: {}['boom']
        command.execute.handlers[0].notify(None)
        self.assertEqual(self.errors, [])
        prompt, title, details = self.details[-1]
        log = self.log / 'errors.log'
        self.assertIn(str(log), prompt)
        self.assertIn("KeyError: 'boom'", details)
        self.assertNotIn('\n', details)
        self.assertIn("KeyError: 'boom'", log.read_text())

    def test_old_fusion_offers_everything_only(self):
        self.module.run(None)
        command = NS(execute=Event(), inputChanged=Event(), validateInputs=Event(), incomingFromHTML=Event(),
                     commandInputs=fake.OldInputs(),
                     setDialogInitialSize=lambda w, h: None, setDialogMinimumSize=lambda w, h: None)
        self.definition.commandCreated.handlers[-1].notify(NS(command=command))
        self.assertEqual(self.errors, [])
        inputs = command.commandInputs
        self.assertEqual([item.name for item in inputs.find('fbx_mode').listItems], ['Everything'])
        self.assertIn('needs a newer Fusion', inputs.find('fbx_old').text)
        self.assertIsNone(inputs.find('fbx_tree'))
        self.change(command, inputs.find('fbx_mode'))
        self.assertTrue(self.valid(command))
        command.execute.handlers[0].notify(None)
        self.assertEqual(self.exported(), ['file-1'])


class ErrorMappingTests(unittest.TestCase):
    def test_trouble_codes_and_known_failures(self):
        import errno
        from fusion_bulk_export import errors
        from fusion_bulk_export.core import Trouble
        for code, words in (('disk_full', 'disk space'), ('output_missing', 'Reconnect'),
                            ('output_unwritable', 'write to'), ('failing', 'signed in')):
            known = errors.friendly(Trouble(code, 'Reason.'))
            self.assertEqual(known.message, 'The export stopped: Reason.')
            self.assertIn(words, known.next_step)
        self.assertIn('disk space', errors.friendly(OSError(errno.ENOSPC, 'No space left on device')).next_step)
        self.assertIn('Sign in', errors.friendly(TimeoutError('No hubs appeared; is Fusion signed in?')).text())
        self.assertIsNone(errors.friendly(KeyError('x')))
        self.assertIsNone(errors.output_folder(ValueError('something else'), '/x'))
        self.assertIn('cannot be used', errors.output_folder(PermissionError(13, 'Permission denied'), '/x').text())


class ProgressTextTests(unittest.TestCase):
    def state(self, **changes):
        state = dict(phase='listing', project=3, projects=17, project_name='P', path=['A', 'B'], design=40,
                     name=None, folder_files=None, folder_item=0, elapsed=725, seconds_per_design=None,
                     counts=dict(exported=0, up_to_date=0, files=0, skipped=0, failed=2))
        state.update(changes)
        return state

    def test_listing_and_design_lines(self):
        from fusion_bulk_export.command import describe
        self.assertEqual(describe(self.state()),
                         'Project 3 of 17, listing: P / A / B\n40 design(s) so far\n12 min · 2 failed')
        state = self.state(phase='design', design=41, name='Gear 50%', seconds_per_design=3.42, elapsed=4000)
        self.assertEqual(describe(state),
                         'Project 3 of 17, design 41: Gear 50%%\nP / A / B\n1 h 06 min · 3.4 s/design · 2 failed')

    def test_bar_moves_within_a_folder_of_a_single_project(self):
        from fusion_bulk_export.command import bar
        single = dict(projects=1, project=1, phase='design', folder_files=4)
        self.assertEqual([bar(dict(single, folder_item=n))[1] for n in (1, 2, 3, 4)], [0, 250, 500, 750])
        self.assertEqual(bar(dict(single, folder_item=1))[0], 1000)
        # Unknown file count: whole projects only.
        self.assertEqual(bar(dict(projects=3, project=2, phase='design', folder_files=None, folder_item=9)),
                         (3000, 1000))
        self.assertEqual(bar(dict(projects=0, project=0, phase='listing')), (1000, 0))


class ProgressStateTests(unittest.TestCase):
    def test_engine_reports_folder_position_rate_and_failures(self):
        from fusion_bulk_export.core import Exporter
        from fusion_bulk_export.fusion import FusionAdapter
        files = [fake.File('F%d' % n, 'f%d' % n) for n in range(3)]
        files[1].failures = 2
        sub = fake.folder('Sub', files=[fake.File('S', 's')])
        data = fake.data(files, [sub])
        states = []
        ticks = iter(range(0, 10000, 2))
        with tempfile.TemporaryDirectory() as out:
            exporter = Exporter(out, FusionAdapter(NS(documents=fake.Documents())))
            exporter.clock = lambda: next(ticks)
            exporter.run(data, progress=states.append)
            again = []
            Exporter(out, FusionAdapter(NS(documents=fake.Documents()))).run(data, progress=again.append)
        designs = [s for s in states if s['phase'] == 'design']
        self.assertEqual([(s['folder_item'], s['folder_files']) for s in designs], [(1, 3), (2, 3), (3, 3), (1, 1)])
        self.assertEqual(designs[2]['counts']['failed'], 1)
        self.assertEqual(designs[0]['seconds_per_design'], None)
        self.assertGreater(designs[1]['seconds_per_design'], 0)
        self.assertGreater(designs[-1]['elapsed'], designs[0]['elapsed'])
        listing = [s for s in states if s['phase'] == 'listing']
        self.assertEqual([s['folder_files'] for s in listing], [3, 1])
        # Up-to-date designs are not timed.
        self.assertIsNone([s for s in again if s['phase'] == 'design'][1]['seconds_per_design'])
